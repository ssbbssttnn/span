#!/usr/bin/env python3
"""
fetch_photos.py — auto-source stretch photos for Float (or any Limber-family app)

What it does
------------
1. Reads photo-filenames.txt (the cheat-sheet) to learn every stretch's
   filename (= stretch id) and display name.
2. Pulls liftmanual's exercise index once.
3. For each stretch it tries, in order:
      a) an entry in OVERRIDES below (you pin these by hand)
      b) exact title match
      c) filename-as-slug match (e.g. "pigeon" / "pigeon-hip-stretch" / "pigeon-pose")
      d) fuzzy title match (kept only above a confidence cutoff)
4. For each match it opens the page, reads the og:image (the clean static photo),
   downloads it, and saves it as photos/<filename>.
5. Writes fetch-report.csv: status, confidence, what it matched, what failed.

Idempotent: a stretch that already has photos/<id>.* is skipped unless --force.

Usage
-----
    python3 fetch_photos.py                # match + download everything missing
    python3 fetch_photos.py --dry-run      # match only, write report, download nothing
    python3 fetch_photos.py --force        # re-download even if a photo exists
    python3 fetch_photos.py --limit 20     # only the first 20 (for testing)
    python3 fetch_photos.py --min-confidence 0.90

Standard library only, no pip installs needed.

RIGHTS: liftmanual.com content is "(c) Lift Manual, all rights reserved." This
downloads their images for your app. That's your call; the script is polite
(normal User-Agent, rate-limited) but gives you no licence.
"""

import argparse
import csv
import difflib
import html
import os
import re
import sys
import time
import urllib.request
import urllib.error

INDEX_URL = "https://liftmanual.com/exercises/"
UA = "Mozilla/5.0 (compatible; SpanPhotoFetch/1.0; personal use)"
DELAY = 1.0  # seconds between network requests

HERE = os.path.dirname(os.path.abspath(__file__))
CHEATSHEET = os.path.join(HERE, "photo-filenames.txt")
PHOTOS_DIR = os.path.join(HERE, "photos")
REPORT = os.path.join(HERE, "fetch-report.csv")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# --- Manual overrides -------------------------------------------------------
# If a stretch matches the wrong page (or none), pin it here:
#     "<filename>": "<full liftmanual URL>"
# Find the right URL by searching the stretch on liftmanual.com.
OVERRIDES = {
    # "deep-squat.jpg": "https://liftmanual.com/full-squat-mobility/",
    # "frog-rock.jpg":  "https://liftmanual.com/rocking-ankle-stretch/",
}
# ---------------------------------------------------------------------------


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def normalize(s):
    s = html.unescape(s).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_cheatsheet(path):
    """Returns list of (filename, display_name). Lines look like:
         pigeon.jpg                          Pigeon Hip Stretch  [static, both sides]
    """
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s+(\S+\.(?:jpg|jpeg|png|webp))\s+(.+?)(?:\s{2,}\[.*\])?\s*$", line)
            if m:
                fname = m.group(1).strip()
                name = m.group(2).strip()
                out.append((fname, name))
    return out


def parse_index(htmltext):
    """Returns list of (title, url) for every exercise/stretch link on the index."""
    pairs = []
    for m in re.finditer(r'<a[^>]+href="(https://liftmanual\.com/[^"]+/)"[^>]*>([^<]+)</a>', htmltext):
        url, title = m.group(1), html.unescape(m.group(2)).strip()
        if title and "/exercises" not in url:
            pairs.append((title, url))
    # dedupe, keep first
    seen, uniq = set(), []
    for t, u in pairs:
        if u in seen:
            continue
        seen.add(u)
        uniq.append((t, u))
    return uniq


def slug_variants(filename):
    base = re.sub(r"\.(jpg|jpeg|png|webp)$", "", filename)
    base = base.lower()
    variants = {base}
    variants.add(base + "-stretch")
    variants.add(base + "-pose")
    variants.add(base + "-yoga-pose")
    variants.add(base.replace("-", " "))
    return variants


def url_slug(url):
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def find_match(filename, name, index, index_norm, min_conf):
    # a) override
    if filename in OVERRIDES:
        return OVERRIDES[filename], 1.0, "override"
    nname = normalize(name)
    # b) exact title match
    for (title, url), tnorm in zip(index, index_norm):
        if tnorm == nname:
            return url, 1.0, "exact"
    # c) filename-as-slug match
    variants = slug_variants(filename)
    for (title, url) in index:
        if url_slug(url) in variants:
            return url, 0.97, "slug"
    # d) fuzzy
    best, best_conf = None, 0.0
    for (title, url), tnorm in zip(index, index_norm):
        c = difflib.SequenceMatcher(None, nname, tnorm).ratio()
        if c > best_conf:
            best_conf, best = c, url
    if best and best_conf >= min_conf:
        return best, round(best_conf, 3), "fuzzy"
    return None, round(best_conf, 3), "UNMATCHED"


def extract_og_image(htmltext):
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', htmltext)
    if m:
        return html.unescape(m.group(1))
    m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', htmltext)
    if m:
        return html.unescape(m.group(1))
    return None


def already_has_photo(base):
    for ext in IMG_EXTS:
        if os.path.exists(os.path.join(PHOTOS_DIR, base + ext)):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-confidence", type=float, default=0.86)
    args = ap.parse_args()

    if not os.path.exists(CHEATSHEET):
        sys.exit("ERROR: photo-filenames.txt not found in repo root.")
    os.makedirs(PHOTOS_DIR, exist_ok=True)

    stretches = parse_cheatsheet(CHEATSHEET)
    if args.limit:
        stretches = stretches[: args.limit]
    print(f"Loaded {len(stretches)} stretches from cheat-sheet.")

    print("Fetching liftmanual index…")
    index = parse_index(get(INDEX_URL).decode("utf-8", "replace"))
    index_norm = [normalize(t) for t, _ in index]
    print(f"Index has {len(index)} entries.")

    rows = []
    downloaded = skipped = failed = 0
    for i, (fname, name) in enumerate(stretches, 1):
        base = re.sub(r"\.(jpg|jpeg|png|webp)$", "", fname)
        if already_has_photo(base) and not args.force:
            rows.append([fname, name, "skipped-exists", "", "", ""])
            skipped += 1
            continue
        url, conf, how = find_match(fname, name, index, index_norm, args.min_confidence)
        if not url:
            rows.append([fname, name, "UNMATCHED", conf, "", ""])
            failed += 1
            print(f"[{i}/{len(stretches)}] {name}: UNMATCHED (best {conf})")
            continue
        if args.dry_run:
            rows.append([fname, name, f"would-{how}", conf, url, ""])
            print(f"[{i}/{len(stretches)}] {name}: {how} {conf} -> {url}")
            time.sleep(DELAY)
            continue
        try:
            page = get(url).decode("utf-8", "replace")
            time.sleep(DELAY)
            img = extract_og_image(page)
            if not img:
                rows.append([fname, name, "no-og-image", conf, url, ""])
                failed += 1
                continue
            ext = os.path.splitext(img.split("?")[0])[1].lower()
            if ext not in IMG_EXTS:
                ext = ".jpg"
            data = get(img)
            time.sleep(DELAY)
            with open(os.path.join(PHOTOS_DIR, base + ext), "wb") as fh:
                fh.write(data)
            rows.append([fname, name, f"ok-{how}", conf, url, img])
            downloaded += 1
            print(f"[{i}/{len(stretches)}] {name}: saved {base}{ext} ({how} {conf})")
        except Exception as e:
            rows.append([fname, name, f"error:{e}", conf, url, ""])
            failed += 1
            print(f"[{i}/{len(stretches)}] {name}: ERROR {e}")

    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "name", "status", "confidence", "page_url", "image_url"])
        w.writerows(rows)

    print(f"\nDone. downloaded={downloaded} skipped={skipped} failed={failed}")
    print(f"Report: {REPORT}")
    if args.dry_run:
        print("Dry run — nothing downloaded. Review fetch-report.csv, then run for real.")


if __name__ == "__main__":
    main()

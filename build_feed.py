#!/usr/bin/env python3
"""Build this week's art feed for Aerial Views.

Picks random public-domain paintings from museum open-access APIs (the Met and
the Cleveland Museum of Art), resizes each one to fit a 4K screen, and writes a
small website: img/ (the paintings), feed.csv (for Aerial Views), feed.m3u
(for Projectivy's Overflight plugin) and index.html (a gallery to browse).

The mix of sources lives in sources.json. GitHub Actions runs this weekly.
Local test:  pip install pillow && python build_feed.py --base-url http://localhost:8000
"""
import argparse
import html
import io
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

MET = "https://collectionapi.metmuseum.org/public/collection/v1"
CMA = "https://openaccess-api.clevelandart.org/api/artworks/"
HEADERS = {"User-Agent": "aerialviews-art-feed/1.0 (+github actions)"}
MAX_DOWNLOAD = 150 * 1024 * 1024
MAX_TRIES_PER_PICK = 8          # downloads attempted per painting wanted
MAX_MET_LOOKUPS = 4000          # Met object lookups per source, at most
Image.MAX_IMAGE_PIXELS = 600_000_000  # museum scans are huge; these sources are trusted
LOG_LINES = []                  # also published as build-log.txt


def log(msg):
    print(msg, flush=True)
    LOG_LINES.append(str(msg))


def num(value):
    """Museum APIs send sizes as numbers or as text ("3400"); accept both."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ratio_of(width, height):
    w, h = num(width), num(height)
    return w / h if w and h else None


def fetch(url, tries=3, limit=None):
    """GET a URL and return the bytes, retrying a couple of times."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(limit + 1) if limit else r.read()
            if limit and len(data) > limit:
                raise ValueError("file too large")
            return data
        except Exception as e:
            if attempt == tries - 1 or isinstance(e, ValueError):
                raise
            time.sleep(2 * (attempt + 1))


def get_json(url):
    return json.loads(fetch(url))


def clean(text):
    return re.sub(r"\s+", " ", str(text or "")).replace('"', "'").strip()


def contains_any(text, terms):
    text = text.lower()
    return any(t.lower() in text for t in terms)


# ---------------- candidate generators ----------------
# Each yields dicts: {"key", "url", "description", "ratio"} (ratio may be None).

def met_ratio(o):
    for m in o.get("measurements") or []:
        em = m.get("elementMeasurements") or {}
        r = ratio_of(em.get("Width"), em.get("Height"))
        if r:
            return r
    found = re.search(r"\(([\d.]+)\s*[x×]\s*([\d.]+)\s*cm\)", o.get("dimensions") or "")
    return ratio_of(found.group(2), found.group(1)) if found else None


def met_candidates(src, ids):
    ids = list(dict.fromkeys(ids))
    random.shuffle(ids)
    log(f"  {len(ids)} Met objects to sample from")
    failed_in_a_row = 0
    for oid in ids[:MAX_MET_LOOKUPS]:
        try:
            o = get_json(f"{MET}/objects/{oid}")
            failed_in_a_row = 0
        except Exception as e:
            failed_in_a_row += 1
            if failed_in_a_row >= 15:
                raise RuntimeError(f"the Met stopped answering object lookups ({e})")
            continue
        time.sleep(0.05)  # stay well under the Met's 80 requests/second
        if not (o.get("isPublicDomain") and o.get("primaryImage")):
            continue
        kind = f"{o.get('classification', '')} {o.get('objectName', '')}"
        if src.get("classification") and src["classification"].lower() not in kind.lower():
            continue
        if src.get("kind_terms") and not contains_any(kind, src["kind_terms"]):
            continue
        if src.get("culture_terms"):
            place = " ".join(str(o.get(f) or "") for f in (
                "culture", "country", "region", "subregion", "state", "period", "dynasty"))
            if not contains_any(place, src["culture_terms"]):
                continue
        parts = (o.get("title"), o.get("artistDisplayName"), o.get("objectDate"))
        yield {"key": f"met-{oid}", "url": o["primaryImage"],
               "description": " - ".join(clean(p) for p in parts if p),
               "ratio": met_ratio(o)}


def met_department(src):
    ids = []
    for d in src["departments"]:
        ids += get_json(f"{MET}/objects?departmentIds={d}").get("objectIDs") or []
    yield from met_candidates(src, ids)


def met_search(src):
    ids = []
    for d in src["departments"]:
        for q in src["queries"]:
            params = urllib.parse.urlencode({"departmentId": d, "hasImages": "true", "q": q})
            try:
                found = get_json(f"{MET}/search?{params}").get("objectIDs") or []
            except Exception as e:
                log(f"  Met search for '{q}' in department {d} failed: {e}")
                found = []
            log(f"  Met search '{q}' in department {d}: {len(found)} matches")
            ids += found
            time.sleep(0.2)
    if not ids:  # search is down or found nothing: sample the whole departments instead
        log("  falling back to scanning the full department lists")
        for d in src["departments"]:
            ids += get_json(f"{MET}/objects?departmentIds={d}").get("objectIDs") or []
    yield from met_candidates(src, ids)


def cma(src):
    items, skip, page = [], 0, 500
    while True:
        params = urllib.parse.urlencode({
            "has_image": 1, "type": src.get("artwork_type", "Painting"),
            "department": src["department"], "limit": page, "skip": skip,
            "fields": "id,title,creators,creation_date,culture,technique,images"})
        batch = get_json(f"{CMA}?cc0&{params}").get("data") or []
        items += batch
        skip += len(batch)
        if len(batch) < page:
            break
        time.sleep(0.3)
    log(f"  {len(items)} Cleveland works to sample from")
    random.shuffle(items)
    for a in items:
        img = (a.get("images") or {}).get("print") or {}
        if not img.get("url"):
            continue
        if src.get("culture_terms") and not contains_any(" ".join(a.get("culture") or []), src["culture_terms"]):
            continue
        if src.get("text_terms") and not contains_any(
                f"{a.get('title', '')} {a.get('technique', '')}", src["text_terms"]):
            continue
        creators = a.get("creators") or []
        artist = creators[0].get("description") if creators else None
        parts = (a.get("title"), artist, a.get("creation_date"))
        yield {"key": f"cma-{a.get('id')}", "url": img["url"],
               "description": " - ".join(clean(p) for p in parts if p),
               "ratio": ratio_of(img.get("width"), img.get("height"))}


GENERATORS = {"met-department": met_department, "met-search": met_search, "cma": cma}


# ---------------- images ----------------

def save_resized(data, max_size, quality, out_file, thumb_file, lo, hi):
    """Fit the image inside max_size, save as JPEG, and make a gallery thumbnail.
    Returns False (and saves nothing) if the picture's shape is outside lo..hi."""
    with Image.open(io.BytesIO(data)) as im:
        if not (lo <= im.width / im.height <= hi):
            return False
        im.draft("RGB", tuple(max_size))  # fast downscale while decoding JPEGs
        im = im.convert("RGB")
        im.thumbnail(tuple(max_size), Image.LANCZOS)
        for q in (quality, quality - 10):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
            if buf.tell() <= 2_500_000:
                break
        out_file.write_bytes(buf.getvalue())
        im.thumbnail((480, 480), Image.LANCZOS)
        im.save(thumb_file, "JPEG", quality=80)
    return True


def build_source(src, cfg, out, exclude, base_url):
    want = int(src.get("count", 10))
    lo, hi = float(src.get("min_ratio", 1.15)), float(src.get("max_ratio", 3.0))
    rows, tries, note = [], 0, ""
    try:
        for c in GENERATORS[src["type"]](src):
            if c["key"] in exclude:
                continue
            # The catalogue shape is a rough guide (a pair of screens may be photographed
            # together), so only skip clear misses here and check real pixels below.
            if c["ratio"] and not (lo * 0.75 <= c["ratio"] <= hi * 1.5):
                continue
            tries += 1
            if tries > want * MAX_TRIES_PER_PICK:
                break
            try:
                data = fetch(c["url"], limit=MAX_DOWNLOAD)
                fits = save_resized(data, cfg["max_size"], cfg["jpeg_quality"],
                                    out / "img" / f"{c['key']}.jpg",
                                    out / "thumbs" / f"{c['key']}.jpg", lo, hi)
            except Exception as e:
                log(f"    skipped {c['key']}: {e}")
                continue
            if not fits:
                continue
            exclude.add(c["key"])
            rows.append({"key": c["key"], "url": f"{base_url}/img/{c['key']}.jpg",
                         "description": c["description"], "source": src["name"]})
            log(f"    [{len(rows)}/{want}] {c['description'][:90]}")
            if len(rows) >= want:
                break
    except Exception as e:  # the API is down or changed: keep going with other sources
        note = f"stopped early: {type(e).__name__}: {e}"
        log(f"  {src['name']} {note}")
    if not note and len(rows) < want:
        note = "ran out of suitable works"
    log(f"  => {src['name']}: {len(rows)} of {want}{' (' + note + ')' if note else ''}")
    return rows, note


def last_week_keys(base_url):
    try:
        text = fetch(f"{base_url}/feed.csv", tries=1).decode("utf-8", "replace")
    except Exception:
        return set()
    return set(re.findall(r"/img/([\w-]+)\.jpg", text))


def write_site(out, rows, base_url, summary):
    lines = ["url,description"] + [f'{r["url"]},"{r["description"]}"' for r in rows]
    (out / "feed.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    m3u = ["#EXTM3U"] + [f'#EXTINF:-1,{r["description"]}\n{r["url"]}' for r in rows]
    (out / "feed.m3u").write_text("\n".join(m3u) + "\n", encoding="utf-8")
    cards = "\n".join(
        f'<a href="img/{r["key"]}.jpg"><img loading="lazy" src="thumbs/{r["key"]}.jpg" alt="">'
        f'<span>{html.escape(r["description"])}<br><small>{html.escape(r["source"])}</small></span></a>'
        for r in rows)
    sources = "".join(
        f"<li>{html.escape(name)}: {got} of {want}"
        f"{' <small>(' + html.escape(note) + ')</small>' if note else ''}</li>"
        for name, got, want, note in summary)
    (out / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>This week's paintings</title>
<style>
body{{margin:0;padding:16px;background:#111;color:#eee;font:15px/1.4 system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}} p{{margin:0 0 16px;color:#aaa}} a{{color:inherit;text-decoration:none}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.grid img{{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:6px;background:#222}}
.grid span{{display:block;margin-top:6px;font-size:13px}} small{{color:#888}}
ul{{margin:0 0 16px;padding-left:20px;color:#aaa}} p a,ul a{{text-decoration:underline}}
</style></head><body>
<h1>This week's paintings</h1>
<p>{len(rows)} works, built {time.strftime('%Y-%m-%d')}. Aerial Views feed: <a href="feed.csv">{html.escape(base_url)}/feed.csv</a> · <a href="build-log.txt">build log</a></p>
<ul>{sources}</ul>
<div class="grid">
{cards}
</div></body></html>
""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--out", default="site")
    ap.add_argument("--base-url", required=True, help="where the site will be published")
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    cfg.setdefault("max_size", [3840, 2160])
    cfg.setdefault("jpeg_quality", 85)
    base_url = a.base_url.rstrip("/")
    out = Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    (out / "thumbs").mkdir(parents=True, exist_ok=True)

    exclude = last_week_keys(base_url) if cfg.get("skip_last_week", True) else set()
    log(f"Skipping {len(exclude)} paintings used last week")

    rows, summary = [], []
    for src in cfg["sources"]:
        log(f"{src['name']}: want {src.get('count', 10)}")
        got, note = build_source(src, cfg, out, exclude, base_url)
        rows += got
        summary.append((src["name"], len(got), int(src.get("count", 10)), note))

    log("Summary:")
    for name, got, want, note in summary:
        log(f"  {name}: {got} of {want}{' (' + note + ')' if note else ''}")
    if len(rows) < cfg.get("min_total", 10):
        log(f"Only {len(rows)} paintings found; not publishing, so last week's set stays up.")
        sys.exit(1)
    random.shuffle(rows)
    write_site(out, rows, base_url, summary)
    size = sum(f.stat().st_size for f in (out / "img").iterdir()) / 1e6
    log(f"Done: {len(rows)} paintings, {size:.0f} MB of images")
    (out / "build-log.txt").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

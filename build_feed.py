#!/usr/bin/env python3
"""Build this week's art feed for Aerial Views.

Picks random public-domain paintings from museum open-access APIs (the Met and
the Cleveland Museum of Art), crops each one to exactly 16:9 at 4K, and writes a
small website:
  feed-4k.csv  60-second 4K video clips (video/), which Google TV shows at full 4K
  feed.csv     photos (img/) sized for the TV's 1080p interface layer
  feed.m3u     the photos, for Projectivy's Overflight plugin
  index.html   a gallery to browse, plus build-log.txt

The mix of sources lives in sources.json. GitHub Actions runs this weekly.
Local test:  pip install pillow (and install ffmpeg) && python build_feed.py --base-url http://localhost:8000
"""
import argparse
import html
import io
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

MET = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
CMA = "https://openaccess-api.clevelandart.org/api/artworks/"
HEADERS = {"User-Agent": "aerialviews-art-feed/1.0 (+github actions)"}
MAX_DOWNLOAD = 2 * 1024 ** 3   # Cleveland's full scans are uncompressed TIFFs; big screens exceed 300 MB
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


def safe_url(url):
    """Some Met image links contain spaces ('.../SF 58.189.1.jpg'); escape them."""
    return urllib.parse.quote(url, safe=":/?&=%#+,;@~")


class TooLarge(Exception):
    pass


def download(url, dest, limit=MAX_DOWNLOAD, tries=3):
    """Stream a (possibly huge) image to disk instead of holding it in memory."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(safe_url(url), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                size = 0
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise TooLarge()
                    f.write(chunk)
            return
        except Exception as e:
            if attempt == tries - 1 or isinstance(e, TooLarge):
                raise
            time.sleep(2 * (attempt + 1))


def clean(text):
    return re.sub(r"\s+", " ", str(text or "")).replace('"', "'").strip()


def contains_any(text, terms):
    text = text.lower()
    return any(t.lower() in text for t in terms)


CAPTION_MAX = 80


def short_title(title):
    """Drop the series part of long manuscript titles:
    'Rama ... Sita: Illustrated folio from a dispersed Ramayana series' -> 'Rama ... Sita'."""
    t = clean(title)
    head, sep, tail = t.partition(":")
    if sep and head.strip() and re.search(r"\b(folio|page|leaf|from an?|from the)\b", tail, re.I):
        return head.strip()
    return t


def short_artist(artist):
    """'Kano Tan'yū (Japanese, 1602–1674)' -> 'Kano Tan'yū'."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", clean(artist)).strip()


def caption(title, artist, date):
    """'Title — Artist, Date'. Missing parts are simply left out; never a placeholder."""
    t, a, d = short_title(title), short_artist(artist), clean(date)

    def build(t):
        base = f"{t} — {a}" if t and a else (t or a)
        return f"{base}, {d}" if base and d else (base or d)

    text = build(t)
    if len(text) > CAPTION_MAX:
        room = CAPTION_MAX - (len(text) - len(t)) - 1
        if t and room >= 20:  # shorten the title, keep artist and date whole
            text = build(t[:room].rstrip(" ,;:—-") + "…")
        else:
            text = text[:CAPTION_MAX - 1].rstrip() + "…"
    return text


def series_of(title):
    """The manuscript or series a single page belongs to, or None:
    'Text, Folio 52 (verso), from a Kalpa-sutra' -> 'kalpasutra'."""
    t = clean(title)
    if not re.search(r"\b(folio|page|leaf|text)\b", t, re.I):
        return None
    m = re.search(r"\bfrom (?:an?|the)\s+(.+)$", t, re.I)
    if not m:
        return None
    s = re.sub(r"\([^)]*\)", " ", m.group(1).lower())
    s = re.sub(r"\b(dispersed|illustrated|manuscript|series|set|album|the|a|an|of)\b", " ", s)
    return re.sub(r"[^a-z]", "", s) or None


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
        if src.get("exclude_terms"):
            about = " ".join(str(o.get(f) or "") for f in (
                "culture", "country", "region", "subregion", "state", "period", "dynasty",
                "department", "classification", "objectName", "title"))
            if contains_any(about, src["exclude_terms"]):
                continue
        yield {"key": f"met-{oid}", "url": o["primaryImage"], "title": o.get("title") or "",
               "description": caption(o.get("title"), o.get("artistDisplayName"), o.get("objectDate")),
               "ratio": met_ratio(o)}


def met_department(src):
    ids = []
    for d in src["departments"]:
        ids += get_json(f"{MET}/objects?departmentIds={d}").get("objectIDs") or []
    yield from met_candidates(src, ids)


def met_search_ids(department, query):
    """All matching IDs from the Met's paged search (v1.1; v1/search was retired
    on 2026-10-01). Pages hold up to 500 IDs; the Met stops at 10,000 results."""
    ids, offset, page = [], 0, 500
    while offset < 10_000:
        params = urllib.parse.urlencode({"departmentId": department, "hasImages": "true",
                                         "q": query, "offset": offset, "limit": page})
        data = get_json(f"{MET_SEARCH}?{params}")
        batch = data.get("objectIDs") or []
        ids += batch
        offset += page
        if len(batch) < page or offset >= (data.get("total") or 0):
            break
        time.sleep(0.2)
    return ids


def met_search(src):
    ids = []
    for d in src["departments"]:
        for q in src["queries"]:
            try:
                found = met_search_ids(d, q)
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
    size = src.get("image", "full")  # "full" = original scan (TIFF), "print" = 3400px JPEG
    for a in items:
        imgs = a.get("images") or {}
        img = imgs.get(size) or imgs.get("print") or {}
        if not img.get("url"):
            continue
        prt = imgs.get("print") or {}
        # The 3400px print JPEG is a light stand-in when the TIFF is too big or not needed.
        alt = ({"url": prt["url"], "pixels": (num(prt.get("width")), num(prt.get("height")))}
               if prt.get("url") and prt.get("url") != img["url"] else None)
        if src.get("culture_terms") and not contains_any(" ".join(a.get("culture") or []), src["culture_terms"]):
            continue
        if src.get("text_terms") and not contains_any(
                f"{a.get('title', '')} {a.get('technique', '')}", src["text_terms"]):
            continue
        if src.get("exclude_terms") and contains_any(
                f"{' '.join(a.get('culture') or [])} {a.get('title', '')} {a.get('technique', '')}",
                src["exclude_terms"]):
            continue
        creators = a.get("creators") or []
        artist = creators[0].get("description") if creators else None
        yield {"key": f"cma-{a.get('id')}", "url": img["url"], "title": a.get("title") or "",
               "description": caption(a.get("title"), artist, a.get("creation_date")),
               "ratio": ratio_of(img.get("width"), img.get("height")),
               "pixels": (num(img.get("width")), num(img.get("height"))),
               "bytes": num(img.get("filesize")), "alt": alt}


GENERATORS = {"met-department": met_department, "met-search": met_search, "cma": cma}


# ---------------- images ----------------

def detail_scale(width, height, out_w, out_h, crop):
    """How many source pixels per screen pixel the picture has (1.0 = exactly 4K)."""
    if crop:  # the cropped area must cover the whole screen
        return min(width / out_w, height / out_h)
    return max(width / out_w, height / out_h)  # fitted inside the screen


def make_video(im, path, size, vcfg):
    """Turn a still picture into a 4K H.264 clip. Only a frame every 10 seconds is
    stored (the picture never changes), so it encodes in seconds and stays small."""
    w, h = size
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        im.save(frame, "PNG", compress_level=1)
        vf = (f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2," if im.size != (w, h) else "")
        vf += "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-loop", "1", "-framerate", "0.1", "-i", str(frame),
               "-t", str(vcfg.get("seconds", 60)), "-vf", vf,
               "-c:v", "libx264", "-preset", "slow", "-tune", "stillimage",
               "-crf", str(vcfg.get("crf", 16)), "-bf", "0",
               "-profile:v", "high", "-level", "5.1",
               "-x264-params", "keyint=infinite:scenecut=0",
               "-colorspace", "bt709", "-color_primaries", "bt709",
               "-color_trc", "bt709", "-color_range", "tv",
               "-an", "-movflags", "+faststart", str(path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-300:]}")


def remove_outputs(out, key):
    for sub, ext in (("img", "jpg"), ("thumbs", "jpg"), ("video", "mp4")):
        (out / sub / f"{key}.{ext}").unlink(missing_ok=True)


def quality_tier(scale, cfg):
    """'4k' if the scan fills the screen without upscaling, 'backup' if it is still
    sharper than 1080p, else None."""
    if scale >= cfg.get("min_source_scale", 1.0):
        return "4k"
    if scale > cfg.get("fallback_min_scale", 0.5):  # 0.5 of 3840 px = 1080p: strictly above
        return "backup"
    return None


def save_resized(source, cfg, out, key, lo, hi, allow_backup=True):
    """Crop (or fit) the picture to the screen, then write the photo, the 4K video
    clip and a gallery thumbnail. Returns (outcome, scale): outcome is '4k' or
    'backup' if saved, otherwise why it was skipped."""
    out_w, out_h = cfg["max_size"]
    crop = cfg.get("crop_to_fill", True)
    with Image.open(source) as im:
        w, h = im.size
        if not (lo <= w / h <= hi):
            return "wrong shape", None
        s = detail_scale(w, h, out_w, out_h, crop)
        tier = quality_tier(s, cfg)
        if tier is None:
            return "too low-res", s
        if tier == "backup" and not allow_backup:
            return "not needed", s
        # Decode big JPEGs at a reduced size, but never below what the screen needs.
        im.draft("RGB", (math.ceil(w / s), math.ceil(h / s)))
        if im.mode != "RGB":  # (converting an RGB image would just copy it; huge scans add up)
            im = im.convert("RGB")
        if crop:  # centre-crop to exactly the screen's shape, then scale to 4K
            target = out_w / out_h
            if im.width / im.height > target:
                cw, ch = round(im.height * target), im.height
            else:
                cw, ch = im.width, round(im.width / target)
            left, top = (im.width - cw) // 2, (im.height - ch) // 2
            im = im.crop((left, top, left + cw, top + ch)).resize((out_w, out_h), Image.LANCZOS)
        else:
            im.thumbnail((out_w, out_h), Image.LANCZOS)
        if cfg["video"].get("enabled"):
            make_video(im, out / "video" / f"{key}.mp4", (out_w, out_h), cfg["video"])
        # Aerial Views draws photos at the TV's interface size (1080p on Google TV),
        # so the photo feed is stored at that size.
        photo = im.copy()
        photo.thumbnail(tuple(cfg["photo_size"]), Image.LANCZOS)
        quality = cfg.get("jpeg_quality", 92)
        for q in (quality, quality - 6):
            buf = io.BytesIO()
            # subsampling=0 keeps full colour detail (4:4:4) for fine brushwork
            photo.save(buf, "JPEG", quality=q, subsampling=0, optimize=True, progressive=True)
            if buf.tell() <= cfg.get("max_file_mb", 6) * 1e6:
                break
        (out / "img" / f"{key}.jpg").write_bytes(buf.getvalue())
        photo.thumbnail((480, 480), Image.LANCZOS)
        photo.save(out / "thumbs" / f"{key}.jpg", "JPEG", quality=80)
    return tier, s


def build_source(src, cfg, out, chosen, last_week, base_url, series_count=None):
    """Pick this source's paintings in order of preference:
    1. new works whose scan fills 4K without upscaling,
    2. if short: new works below 4K but still sharper than 1080p (sharpest first),
    3. if still short: works shown last week (when repeat_last_week_if_short is on).
    Script-only pages are skipped, and at most max_per_series pages come from any one
    manuscript (shared across sources through series_count)."""
    want = int(src.get("count", 10))
    lo, hi = float(src.get("min_ratio", 1.15)), float(src.get("max_ratio", 3.0))
    out_w, out_h = cfg["max_size"]
    crop = cfg.get("crop_to_fill", True)
    repeats_ok = cfg.get("repeat_last_week_if_short", True)
    skip_prefixes = tuple(p.lower() for p in cfg.get("skip_titles_starting_with", ["Text,"]))
    max_series = int(cfg.get("max_per_series", 2))
    series_count = series_count if series_count is not None else {}
    rows, backups, later, note = [], [], [], ""
    tries = 0
    rejected = {"wrong shape": 0, "too low-res": 0, "too large": 0, "not needed": 0,
                "text page": 0, "series limit": 0}

    def series_full(series):
        return bool(series) and series_count.get(series, 0) >= max_series
    tmp = tempfile.TemporaryDirectory()
    source_file = Path(tmp.name) / "source"
    video = cfg["video"].get("enabled")

    def make_row(c, tier, scale, repeat):
        return {"key": c["key"], "url": f"{base_url}/img/{c['key']}.jpg",
                "video": f"{base_url}/video/{c['key']}.mp4" if video else None,
                "description": c["description"], "source": src["name"],
                "tier": tier, "scale": scale, "repeat": repeat,
                "series": series_of(c.get("title"))}

    def take(row):
        rows.append(row)
        chosen.add(row["key"])
        if row["series"]:
            series_count[row["series"]] = series_count.get(row["series"], 0) + 1
        extra = []
        if row["tier"] == "backup":
            extra.append(f"below 4K, {row['scale'] * out_w:.0f}px wide crop")
        if row["repeat"]:
            extra.append("from last week")
        log(f"    [{len(rows)}/{want}] {row['description'][:90]}"
            + (f" ({', '.join(extra)})" if extra else ""))

    def process(c, allow_backup):
        """Download and convert one work. Returns (outcome, scale)."""
        nonlocal tries
        tries += 1
        try:
            download(c["url"], source_file)
            return save_resized(source_file, cfg, out, c["key"], lo, hi, allow_backup)
        except TooLarge:
            return "too large", None
        except Exception as e:
            remove_outputs(out, c["key"])
            log(f"    skipped {c['key']}: {e}")
            return "error", None
        finally:
            source_file.unlink(missing_ok=True)

    def use_print_instead(c):
        alt = c.get("alt")
        return {**c, "url": alt["url"], "pixels": alt["pixels"], "bytes": None, "alt": None}

    try:
        for c in GENERATORS[src["type"]](src):
            if c["key"] in chosen:
                continue
            if (c.get("title") or "").strip().lower().startswith(skip_prefixes):
                rejected["text page"] += 1  # a page of script, not a picture
                continue
            if series_full(series_of(c.get("title"))):
                rejected["series limit"] += 1
                continue
            # The catalogue shape is a rough guide (a pair of screens may be photographed
            # together), so only skip clear misses here and check real pixels below.
            if c["ratio"] and not (lo * 0.92 <= c["ratio"] <= hi * 1.5):
                continue
            if c.get("bytes") and c["bytes"] > MAX_DOWNLOAD and c.get("alt"):
                rejected["too large"] += 1
                c = use_print_instead(c)
            repeat = c["key"] in last_week
            pw, ph = c.get("pixels") or (None, None)
            s = None
            if pw and ph:  # pixel size known up front (Cleveland): decide without downloading
                if not (lo <= pw / ph <= hi):
                    continue
                s = detail_scale(pw, ph, out_w, out_h, crop)
                tier = quality_tier(s, cfg)
                if tier is None:
                    rejected["too low-res"] += 1
                    continue
                if tier == "backup":
                    later.append((1 if not repeat else 2, -s, c))
                    continue
            if repeat:
                later.append((2, -(s or 0), c))
                continue
            if tries >= want * MAX_TRIES_PER_PICK:
                break
            # Only convert below-4K works while the reserve could still be needed.
            outcome, s = process(c, allow_backup=len(backups) < want - len(rows))
            if outcome in ("4k", "backup") and series_full(series_of(c.get("title"))):
                remove_outputs(out, c["key"])  # its manuscript filled up meanwhile
                rejected["series limit"] += 1
            elif outcome == "too large" and c.get("alt"):
                rejected["too large"] += 1
                alt = use_print_instead(c)
                ap = alt["pixels"]
                if ap[0] and ap[1]:
                    later.append((1, -detail_scale(ap[0], ap[1], out_w, out_h, crop), alt))
            elif outcome == "4k":
                take(make_row(c, "4k", s, False))
                if len(rows) >= want:
                    break
            elif outcome == "backup":  # already converted; keep in reserve
                backups.append(make_row(c, "backup", s, False))
            elif outcome in rejected:
                rejected[outcome] += 1
    except Exception as e:  # the API is down or changed: keep going with what we have
        note = f"stopped early: {type(e).__name__}: {e}"
        log(f"  {src['name']} {note}")

    try:
        if len(rows) < want:  # top up: sharpest below-4K works first, then last week's
            queue = [(1, -r["scale"], i, "ready", r) for i, r in enumerate(backups)]
            queue += [(rank, neg, len(queue) + i, "todo", c) for i, (rank, neg, c) in enumerate(later)
                      if rank == 1 or repeats_ok]
            extra_tries = 0
            for rank, _, _, kind, item in sorted(queue):
                if len(rows) >= want:
                    break
                if series_full(item["series"] if kind == "ready" else series_of(item.get("title"))):
                    continue
                if kind == "ready":
                    take(item)
                    continue
                if item["key"] in chosen or extra_tries >= want * MAX_TRIES_PER_PICK:
                    continue
                extra_tries += 1
                outcome, s = process(item, allow_backup=True)
                if outcome in ("4k", "backup"):
                    take(make_row(item, outcome, s, rank == 2))
                elif outcome in rejected:
                    rejected[outcome] += 1
    finally:
        for r in backups:  # converted reserves that weren't needed
            if r["key"] not in chosen:
                remove_outputs(out, r["key"])
        tmp.cleanup()

    n4k = sum(r["tier"] == "4k" for r in rows)
    nrep = sum(r["repeat"] for r in rows)
    detail = f"{n4k} at 4K, {len(rows) - n4k} below 4K" + (f", {nrep} from last week" if nrep else "")
    if not note and len(rows) < want:
        note = "ran out of suitable works"
    log(f"  skipped after checking: {rejected['wrong shape']} wrong shape, "
        f"{rejected['too low-res']} at or below 1080p, {rejected['too large']} TIFFs over 2 GB, "
        f"{rejected['text page']} script-only pages, {rejected['series limit']} over the "
        f"{max_series}-per-manuscript limit")
    log(f"  => {src['name']}: {len(rows)} of {want} ({detail}){' (' + note + ')' if note else ''}")
    return rows, (detail + ("; " + note if note else ""))


def last_week_keys(base_url):
    try:
        text = fetch(f"{base_url}/feed.csv", tries=1).decode("utf-8", "replace")
    except Exception:
        return set()
    return set(re.findall(r"/img/([\w-]+)\.jpg", text))


def write_site(out, rows, base_url, summary):
    lines = ["url,description"] + [f'{r["url"]},"{r["description"]}"' for r in rows]
    (out / "feed.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    videos = [r for r in rows if r.get("video")]
    if videos:
        lines = ["url,description"] + [f'{r["video"]},"{r["description"]}"' for r in videos]
        (out / "feed-4k.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    feeds = (f'4K video feed: <a href="feed-4k.csv">{html.escape(base_url)}/feed-4k.csv</a> · '
             if videos else "")
    feeds += f'photo feed: <a href="feed.csv">{html.escape(base_url)}/feed.csv</a>'
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
<p>{len(rows)} works, built {time.strftime('%Y-%m-%d')}. {feeds} · <a href="build-log.txt">build log</a></p>
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
    cfg.setdefault("photo_size", [1920, 1080])
    cfg.setdefault("jpeg_quality", 92)
    cfg.setdefault("video", {"enabled": True, "seconds": 60, "crf": 16})
    if cfg["video"].get("enabled") and not shutil.which("ffmpeg"):
        log("ffmpeg not found: building photos only, no 4K video feed")
        cfg["video"]["enabled"] = False
    base_url = a.base_url.rstrip("/")
    out = Path(a.out)
    for sub in ("img", "thumbs", "video"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    last_week = last_week_keys(base_url) if cfg.get("skip_last_week", True) else set()
    chosen, series_count = set(), {}
    log(f"{len(last_week)} paintings from last week are only reused if a source runs short"
        if cfg.get("repeat_last_week_if_short", True) else
        f"Skipping {len(last_week)} paintings used last week")

    rows, summary = [], []
    for src in cfg["sources"]:
        # Top-level exclude_terms apply to every source.
        src = {**src, "exclude_terms": src.get("exclude_terms", []) + cfg.get("exclude_terms", [])}
        log(f"{src['name']}: want {src.get('count', 10)}")
        got, note = build_source(src, cfg, out, chosen, last_week, base_url, series_count)
        rows += got
        summary.append((src["name"], len(got), int(src.get("count", 10)), note))

    log("Summary:")
    for name, got, want, note in summary:
        log(f"  {name}: {got} of {want}{' (' + note + ')' if note else ''}")
    if len(rows) < cfg.get("min_total", 10):
        log(f"Only {len(rows)} paintings found; not publishing, so last week's set stays up.")
        sys.exit(1)
    random.shuffle(rows)
    # GitHub Pages sites must stay under 1 GB: drop the largest clips if needed
    # (those paintings stay in the photo feed).
    mb = lambda p: p.stat().st_size / 1e6 if p.exists() else 0
    total = sum(mb(f) for sub in ("img", "thumbs", "video") for f in (out / sub).iterdir())
    for r in sorted((r for r in rows if r["video"]),
                    key=lambda r: -mb(out / "video" / f"{r['key']}.mp4")):
        if total <= cfg.get("max_site_mb", 950):
            break
        clip = out / "video" / f"{r['key']}.mp4"
        total -= mb(clip)
        clip.unlink()
        r["video"] = None
        log(f"  dropped the 4K clip for {r['key']} to stay under GitHub's 1 GB limit")
    write_site(out, rows, base_url, summary)
    photos = sum(mb(f) for f in (out / "img").iterdir())
    clips = sum(mb(f) for f in (out / "video").iterdir())
    log(f"Done: {len(rows)} paintings, {photos:.0f} MB of photos, "
        f"{sum(1 for r in rows if r['video'])} 4K clips ({clips:.0f} MB)")
    (out / "build-log.txt").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

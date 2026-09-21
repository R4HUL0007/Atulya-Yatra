"""Download curated hero photography from Wikimedia Commons into static/images.

Why this exists
---------------
Every curated record already declared a hero image (``images.hero`` such as
``places/vadodara/hero.jpg``, and ``hero_image`` on states), but
``static/images/`` was never created. Those paths were all dangling, so the site
silently fell back to Google Places photos for every image. Places photos are
user submissions: many are low-resolution portrait phone snaps, which is why the
pages looked weak no matter how they were styled.

Wikimedia Commons is the source tourism sites lean on for landmark photography:
high resolution, freely licensed, and reviewable.

Why it downloads instead of hotlinking
--------------------------------------
An earlier version stored Wikimedia URLs directly in the data. Checking those
URLs showed Wikimedia returning HTTP 429 for roughly a quarter of them: they
rate-limit bursts of full-size original fetches, and every visitor's browser
counts. Serving files we host removes the rate limit, the multi-megabyte
originals, and the dependency on someone else's uptime. Attribution is kept in
the data so the licence obligations still travel with the image.

Two widths are saved per record so cards and full-bleed heroes can each load an
appropriate file rather than sharing one oversized image.

Requests are batched (up to 40 titles per API call) because Wikimedia
rate-limits anonymous clients aggressively; a one-request-per-record loop gets
429 within seconds.

Usage
-----
    python scripts/fetch_wiki_images.py --dry-run
    python scripts/fetch_wiki_images.py --apply
    python scripts/fetch_wiki_images.py --apply --states
    python scripts/fetch_wiki_images.py --apply --overwrite
    python scripts/fetch_wiki_images.py --dry-run --report out.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
IMAGE_DIR = BASE_DIR / "static" / "images"

WIKI_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Wikimedia asks automated clients to identify themselves in both headers.
USER_AGENT = (
    "AtulyaYatra/2.0 (India tourism discovery project; "
    "offline hero-image enrichment; contact: repository maintainer)"
)

# MediaWiki accepts up to 50 titles per query for anonymous clients; 40 leaves
# headroom and keeps URLs a sane length.
BATCH_SIZE = 40
BATCH_DELAY = 3.0

# Pacing for the actual file downloads. Wikimedia returns 429 for bursts.
DOWNLOAD_DELAY = 0.6
DOWNLOAD_RETRIES = 4

# Media is only ever accepted from Wikimedia's own hosts over HTTPS, so an
# unexpected response cannot make us download from a third party. Originals come
# from upload.wikimedia.org; generated thumbnails come from thumb.wikimedia.org.
ALLOWED_IMAGE_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}

# The two widths we store: one for cards, one for full-bleed heroes.
CARD_WIDTH = 900
HERO_WIDTH = 1600

# Below this the source image is not worth using as a full-bleed hero.
MIN_WIDTH = 1400
MIN_HEIGHT = 800

# Licences that permit reuse with attribution. Anything else (notably "fair use"
# and unknown values) is skipped rather than guessed at.
ALLOWED_LICENCE_RE = re.compile(
    r"^(cc[\s-]?by([\s-]?sa)?([\s-]?\d(\.\d)?)?|cc[\s-]?0|public domain|pd(-\w+)?)",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Commons artist fields are wiki markup: "Yann <a ...>(talk)</a>" becomes
# "Yann ( talk )" once the tags are stripped. Only the name belongs in a credit.
_ARTIST_NOISE_RE = re.compile(r"\s*\(\s*(talk|talk\s*\|\s*contribs)\s*\)", re.IGNORECASE)

# Lead images that are not photographs of the place.
REJECT_FILE_RE = re.compile(
    r"(flag|coat[_\s-]?of[_\s-]?arms|seal|logo|emblem|map|locator|location|"
    r"\.svg$|\.gif$|icon|banner|montage)",
    re.IGNORECASE,
)

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _clean(value: object) -> str:
    """Strip the HTML that Wikimedia embeds in extmetadata values."""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(value or ""))).strip()


def _strip_query(url: str) -> str:
    """Drop the utm tracking parameters Wikimedia appends to image URLs."""
    parts = urlparse(url)
    return urlunparse(parts._replace(query="", fragment=""))


def _trusted_media_url(url: object) -> Optional[str]:
    clean = _strip_query(str(url or ""))
    parsed = urlparse(clean)
    if parsed.scheme != "https" or parsed.netloc not in ALLOWED_IMAGE_HOSTS:
        return None
    return clean


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Api-User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    return session


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _file_key(name: object) -> str:
    """Normalise a File name for lookup.

    ``pageimages`` reports ``LakshmiVilas_Palace.jpg`` while ``imageinfo`` echoes
    the title back as ``File:LakshmiVilas Palace.jpg``. MediaWiki treats the
    underscore and the space as the same character, so both must key alike.
    """
    return " ".join(str(name or "").replace("_", " ").split())


def title_candidates(name: str) -> list[str]:
    """Article titles worth trying for one curated record name.

    Curated names carry editorial decoration a Wikipedia title will not have:
    "Cherrapunji (Sohra)", "Ajanta and Ellora Caves", "Langza & Komic". Trying a
    few shapes turns most of those into a hit.
    """
    raw = " ".join(str(name or "").split())
    if not raw:
        return []

    candidates: list[str] = []

    def add(value: object) -> None:
        cleaned = " ".join(str(value or "").split()).strip(" ,-&|")
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    outside = re.sub(r"\([^)]*\)", " ", raw)
    add(outside)
    add(raw)
    for inside in re.findall(r"\(([^)]*)\)", raw):
        add(inside)
    for separator in (" and ", " & ", " | ", "-", "/", ","):
        if separator in outside:
            add(outside.split(separator)[0])
    return candidates[:4]


def _request(session: requests.Session, api: str, params: dict, *, retries: int = 4):
    """GET with backoff that honours Retry-After, returning parsed JSON or None."""
    for attempt in range(retries):
        try:
            response = session.get(api, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"    request failed ({exc.__class__.__name__}); retrying", flush=True)
            time.sleep(4 * (attempt + 1))
            continue
        if response.status_code == 429:
            wait = response.headers.get("Retry-After")
            delay = int(wait) if (wait or "").isdigit() else 10 * (attempt + 1)
            print(f"    rate limited; waiting {min(delay, 90)}s", flush=True)
            time.sleep(min(delay, 90))
            continue
        if response.status_code >= 500:
            time.sleep(4 * (attempt + 1))
            continue
        try:
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            return None
    return None


def lead_images(session: requests.Session, titles: list[str]) -> dict[str, dict]:
    """Batch-resolve article titles to their lead-image page data."""
    resolved: dict[str, dict] = {}
    batches = list(_chunks(titles, BATCH_SIZE))
    for index, batch in enumerate(batches, start=1):
        print(f"    lead images batch {index}/{len(batches)} ({len(batch)} titles)", flush=True)
        data = _request(
            session,
            WIKI_API,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "titles": "|".join(batch),
                "redirects": 1,
                "prop": "pageimages",
                "piprop": "original|name",
            },
        )
        if data:
            query = data.get("query") or {}
            # A requested title may be normalised and then redirected before it
            # reaches the page that actually holds the image.
            alias: dict[str, str] = {}
            for group in ("normalized", "redirects"):
                for entry in query.get(group) or []:
                    if entry.get("from") and entry.get("to"):
                        alias[entry["from"]] = entry["to"]
            pages = {
                page.get("title"): page
                for page in (query.get("pages") or [])
                if not page.get("missing") and page.get("title")
            }
            for requested in batch:
                title = requested
                for _ in range(4):
                    if title in pages:
                        resolved[requested] = pages[title]
                        break
                    if title in alias:
                        title = alias[title]
                    else:
                        break
        if index < len(batches):
            time.sleep(BATCH_DELAY)
    return resolved


def file_metadata(
    session: requests.Session, file_names: list[str], width: int
) -> dict[str, dict]:
    """Batch-resolve File: titles to a thumbnail URL plus licence and author."""
    metadata: dict[str, dict] = {}
    pending = list(dict.fromkeys(name for name in file_names if name))

    # Most lead images live on Commons; a few are hosted locally on en.wikipedia.
    for api in (COMMONS_API, WIKI_API):
        if not pending:
            break
        still_missing: list[str] = []
        batches = list(_chunks(pending, BATCH_SIZE))
        host = "commons" if api == COMMONS_API else "en.wikipedia"
        for index, batch in enumerate(batches, start=1):
            print(
                f"    {host} metadata @{width}px batch {index}/{len(batches)}"
                f" ({len(batch)} files)",
                flush=True,
            )
            data = _request(
                session,
                api,
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "titles": "|".join(f"File:{name}" for name in batch),
                    "prop": "imageinfo",
                    "iiprop": "url|size|extmetadata",
                    "iiurlwidth": width,
                    "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Credit",
                },
            )
            found = set()
            if data:
                for page in (data.get("query") or {}).get("pages") or []:
                    if page.get("missing"):
                        continue
                    title = str(page.get("title") or "")
                    raw_name = title.split(":", 1)[1] if ":" in title else title
                    info = (page.get("imageinfo") or [None])[0]
                    if info:
                        metadata[_file_key(raw_name)] = info
                        found.add(_file_key(raw_name))
            still_missing.extend(name for name in batch if _file_key(name) not in found)
            if index < len(batches):
                time.sleep(BATCH_DELAY)
        pending = still_missing

    return metadata


def evaluate(page: dict, info: Optional[dict]) -> tuple[Optional[dict], str]:
    """Turn raw API data into a hero record, or explain why it was rejected."""
    file_name = page.get("pageimage") or ""
    if not file_name:
        return None, "article has no lead image"
    if REJECT_FILE_RE.search(file_name):
        return None, f"lead image is not a photo (File:{file_name})"

    original = page.get("original") or {}
    source_width = int(original.get("width") or 0)
    source_height = int(original.get("height") or 0)
    if source_width < MIN_WIDTH or source_height < MIN_HEIGHT:
        return None, f"too small ({source_width}x{source_height}) File:{file_name}"

    if not info:
        return None, f"no licence metadata for File:{file_name}"

    meta = info.get("extmetadata") or {}
    licence = _clean((meta.get("LicenseShortName") or {}).get("value"))
    if not ALLOWED_LICENCE_RE.match(licence or ""):
        return None, f"licence not reusable ({licence or 'unknown'}) File:{file_name}"

    url = _trusted_media_url(info.get("thumburl") or info.get("url"))
    if not url:
        return None, f"untrusted media host for File:{file_name}"

    artist = _ARTIST_NOISE_RE.sub("", _clean((meta.get("Artist") or {}).get("value"))).strip(" ·,")
    credit_bits = [artist or "Wikimedia Commons", licence]
    return (
        {
            "file_name": file_name,
            "url": url,
            "credit": " · ".join(bit for bit in credit_bits if bit),
            "license": licence,
            "license_url": _clean((meta.get("LicenseUrl") or {}).get("value")),
            "source": "https://commons.wikimedia.org/wiki/File:"
            + file_name.replace(" ", "_"),
            "source_width": source_width,
            "source_height": source_height,
        },
        "ok",
    )


def download(session: requests.Session, url: str, destination: Path) -> Optional[str]:
    """Save one Wikimedia image locally. Returns the written filename or None."""
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            response = session.get(
                url, timeout=60, stream=True, headers={"Accept": "image/*,*/*;q=0.8"}
            )
        except requests.RequestException as exc:
            print(f"      download failed ({exc.__class__.__name__}); retrying", flush=True)
            time.sleep(3 * (attempt + 1))
            continue
        if response.status_code == 429:
            wait = response.headers.get("Retry-After")
            delay = int(wait) if (wait or "").isdigit() else 8 * (attempt + 1)
            print(f"      rate limited; waiting {min(delay, 60)}s", flush=True)
            response.close()
            time.sleep(min(delay, 60))
            continue
        content_type = str(response.headers.get("Content-Type", "")).split(";")[0].strip()
        if response.status_code != 200 or content_type not in CONTENT_TYPE_EXT:
            response.close()
            return None
        target = destination.with_suffix(CONTENT_TYPE_EXT[content_type])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            for block in response.iter_content(chunk_size=65536):
                if block:
                    handle.write(block)
        response.close()
        return target.name
    return None


def _has_local_hero(value: object) -> bool:
    """A hero counts as present only if the file is actually on disk."""
    text = str(value or "")
    if not text or text.startswith("http"):
        return False
    return (IMAGE_DIR / text).is_file()


def collect_targets(*, do_places: bool, do_states: bool, overwrite: bool):
    """Gather every record needing a hero image, grouped by source file."""
    groups = []

    if do_places:
        paths = sorted(DATA_DIR.glob("destinations*.json")) + sorted(
            DATA_DIR.glob("hidden_gems*.json")
        )
        for path in paths:
            records = json.loads(path.read_text(encoding="utf-8"))
            pending = []
            for record in records:
                images = record.get("images")
                if not isinstance(images, dict):
                    images = {}
                    record["images"] = images
                if _has_local_hero(images.get("hero")) and not overwrite:
                    continue
                titles = title_candidates(record.get("name"))
                if titles:
                    pending.append((record, titles))
            groups.append({"path": path, "records": records, "pending": pending, "kind": "place"})

    if do_states:
        path = DATA_DIR / "states.json"
        if path.is_file():
            records = json.loads(path.read_text(encoding="utf-8"))
            pending = []
            for record in records:
                if _has_local_hero(record.get("hero_image")) and not overwrite:
                    continue
                titles = title_candidates(record.get("name"))
                if titles:
                    pending.append((record, titles))
            groups.append({"path": path, "records": records, "pending": pending, "kind": "state"})

    return groups


def apply_hero(record: dict, kind: str, hero: str, card: Optional[str], result: dict) -> None:
    """Point a record at the downloaded files and keep its attribution."""
    holder = record.setdefault("images", {}) if kind == "place" else record
    key = "hero" if kind == "place" else "hero_image"
    holder[key] = hero
    if card:
        holder["hero_small"] = card
    else:
        holder.pop("hero_small", None)
    holder["hero_credit"] = result["credit"]
    holder["hero_credit_url"] = result["source"]
    holder["hero_license_url"] = result["license_url"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Resolve images but download and write nothing (default).")
    parser.add_argument("--apply", action="store_true", help="Download the images and update the data files.")
    parser.add_argument("--overwrite", action="store_true", help="Re-fetch heroes that are already on disk.")
    parser.add_argument("--states", action="store_true", help="Only process data/states.json.")
    parser.add_argument("--places", action="store_true", help="Only process destinations and hidden gems.")
    parser.add_argument("--report", help="Also write the full report to this file.")
    args = parser.parse_args()

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    apply_changes = bool(args.apply)

    do_places = args.places or not args.states
    do_states = args.states or not args.places

    report: list[str] = []
    report_path = Path(args.report) if args.report else None
    if report_path:
        # Written line by line so a long run can be inspected while in progress
        # and partial results survive an interruption.
        report_path.write_text("", encoding="utf-8")

    def say(text: str = "") -> None:
        print(text, flush=True)
        report.append(text)
        if report_path:
            with report_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    groups = collect_targets(do_places=do_places, do_states=do_states, overwrite=args.overwrite)
    total_pending = sum(len(group["pending"]) for group in groups)
    if not total_pending:
        say("Every selected record already has a downloaded hero image. Use --overwrite to refetch.")
        return 0

    say(
        f"Resolving Wikimedia hero images for {total_pending} record(s) across "
        f"{len(groups)} file(s). "
        + ("DOWNLOADING AND WRITING CHANGES." if apply_changes else "Dry run — nothing downloaded or written.")
    )
    say(
        f"Accepting only >= {MIN_WIDTH}x{MIN_HEIGHT} sources under a reusable licence. "
        f"Saving {CARD_WIDTH}px and {HERO_WIDTH}px copies under static/images/."
    )
    say("")

    session = _session()

    all_titles = list(
        dict.fromkeys(title for group in groups for _, titles in group["pending"] for title in titles)
    )
    say(f"Fetching article lead images for {len(all_titles)} candidate title(s)...")
    pages = lead_images(session, all_titles)
    say(f"  {len(pages)} title(s) had a lead image")

    file_names = [
        page.get("pageimage")
        for page in pages.values()
        if page.get("pageimage") and not REJECT_FILE_RE.search(page["pageimage"])
    ]
    say("Fetching licence metadata and hero-size thumbnails...")
    hero_meta = file_metadata(session, file_names, HERO_WIDTH)
    say(f"  {len(hero_meta)} file(s) returned metadata")
    card_meta: dict[str, dict] = {}
    if apply_changes:
        say("Fetching card-size thumbnails...")
        card_meta = file_metadata(session, file_names, CARD_WIDTH)
        say(f"  {len(card_meta)} file(s) returned card thumbnails")
    say("")

    resolved = unresolved = failed_download = 0
    for group in groups:
        if not group["pending"]:
            continue
        say(f"{group['path'].name}:")
        changed = 0
        folder = "places" if group["kind"] == "place" else "states"
        for record, titles in group["pending"]:
            slug = str(record.get("slug") or "").strip()
            result = None
            reasons = []
            for title in titles:
                page = pages.get(title)
                if not page:
                    reasons.append(f"no article for '{title}'")
                    continue
                info = hero_meta.get(_file_key(page.get("pageimage")))
                result, reason = evaluate(page, info)
                if result:
                    break
                reasons.append(reason)
            if not result or not slug:
                unresolved += 1
                say(f"  skip {slug or record.get('name')}: {reasons[0] if reasons else 'no slug'}")
                continue

            if not apply_changes:
                resolved += 1
                say(
                    f"  ok   {slug}: {result['source_width']}x{result['source_height']} "
                    f"[{result['license']}]"
                )
                continue

            rel_dir = f"{folder}/{slug}"
            hero_name = download(session, result["url"], IMAGE_DIR / rel_dir / "hero")
            time.sleep(DOWNLOAD_DELAY)
            if not hero_name:
                failed_download += 1
                say(f"  FAIL {slug}: could not download {result['url'][:90]}")
                continue

            card_name = None
            card_info = card_meta.get(_file_key(result["file_name"]))
            card_url = _trusted_media_url(
                (card_info or {}).get("thumburl") or (card_info or {}).get("url")
            )
            if card_url:
                card_name = download(session, card_url, IMAGE_DIR / rel_dir / "card")
                time.sleep(DOWNLOAD_DELAY)

            resolved += 1
            changed += 1
            apply_hero(
                record,
                group["kind"],
                f"{rel_dir}/{hero_name}",
                f"{rel_dir}/{card_name}" if card_name else None,
                result,
            )
            say(
                f"  ok   {slug}: {result['source_width']}x{result['source_height']} "
                f"[{result['license']}] -> {rel_dir}/{hero_name}"
                + (f" + {card_name}" if card_name else "")
            )

        if apply_changes and changed:
            group["path"].write_text(
                json.dumps(group["records"], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            say(f"  wrote {changed} hero image(s) to {group['path'].name}")
        say("")

    say(f"resolved={resolved} unresolved={unresolved} download_failures={failed_download}")
    if apply_changes:
        say("Images are now served from static/images/, so no visitor request hits Wikimedia.")
    else:
        say("Re-run with --apply to download these images and update data/.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

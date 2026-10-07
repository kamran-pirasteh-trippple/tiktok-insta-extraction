"""Extract caption + metadata + transcript + places from a TikTok URL."""

import argparse
import json
import re
import sys
import time
import urllib.parse
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import net


def resolve_redirects(url: str) -> str:
    try:
        with net.urlopen(url, method="HEAD", timeout=5) as resp:
            return resp.geturl()
    except Exception:
        return url


def fetch_subtitle(url: str) -> str:
    with net.urlopen(url, timeout=10) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_subtitle(body: str) -> str:
    """Handle both WebVTT and TikTok's JSON `utterances` format."""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            return ""
        out: list[str] = []
        for u in data.get("utterances") or []:
            text = (u.get("text") or "").strip()
            if text and (not out or out[-1] != text):
                out.append(text)
        return " ".join(out)

    out_lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("NOTE", "STYLE", "X-TIMESTAMP")):
            continue
        if not out_lines or out_lines[-1] != line:
            out_lines.append(line)
    return " ".join(out_lines)


UNIVERSAL_DATA_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def scrape_page_data(url: str) -> dict:
    """Pull stickers, suggested words, labels, and location from TikTok page HTML."""
    try:
        with net.urlopen(url, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return {}

    m = UNIVERSAL_DATA_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except Exception:
        return {}

    item = (
        data.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct", {})
    )
    if not item:
        return {}

    stickers: list[str] = []
    for sticker in item.get("stickersOnItem") or []:
        for text in sticker.get("stickerText") or []:
            cleaned = " ".join(text.split())
            if cleaned:
                stickers.append(cleaned)

    suggested = item.get("suggestedWords") or []
    if suggested and isinstance(suggested[0], dict):
        suggested = [s.get("word") for s in suggested if s.get("word")]

    return {
        "stickers": stickers,
        "suggested_words": suggested,
        "diversification_labels": item.get("diversificationLabels") or [],
        "location_created": item.get("locationCreated"),
    }


def fetch_comments(aweme_id: str, referer: str, count: int = 50) -> list[dict]:
    """Pull top comments via TikTok's unsigned web comment-list endpoint.

    No auth required. Returns a list of {text, digg_count} dicts, sorted by likes.
    Used only as a fallback when caption/transcript/stickers yield no places.
    """
    if not aweme_id:
        return []
    params = {
        "aweme_id": aweme_id,
        "count": str(count),
        "cursor": "0",
        "aid": "1988",
        "app_name": "tiktok_web",
        "device_platform": "web",
        "os": "mac",
    }
    url = "https://www.tiktok.com/api/comment/list/?" + urllib.parse.urlencode(params)
    try:
        with net.urlopen(url, referer=referer, timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        return []

    out: list[dict] = []
    for c in data.get("comments") or []:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        out.append({"text": text, "digg_count": int(c.get("digg_count") or 0)})
    out.sort(key=lambda c: c["digg_count"], reverse=True)
    return out


def get_transcript(info: dict) -> Optional[str]:
    subs = info.get("subtitles") or {}
    if not subs:
        return None
    lang = next(
        (k for k in subs if k.startswith(("en", "eng"))),
        next(iter(subs), None),
    )
    if not lang or not subs[lang]:
        return None
    try:
        return parse_subtitle(fetch_subtitle(subs[lang][0]["url"]))
    except Exception:
        return None


def extract(url: str) -> dict:
    canonical = resolve_redirects(url)
    opts = net.ydl_opts(writesubtitles=True, subtitleslangs=["en", "eng-US"])
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(canonical, download=False)

    description = info.get("description") or ""
    hashtags = re.findall(r"#(\w+)", description)
    transcript = get_transcript(info)
    page_data = scrape_page_data(canonical)

    return {
        "input_url": url,
        "canonical_url": canonical,
        "id": info.get("id"),
        "caption": description,
        "hashtags": hashtags,
        "transcript": transcript,
        "has_subtitles": transcript is not None,
        "stickers": page_data.get("stickers") or [],
        "suggested_words": page_data.get("suggested_words") or [],
        "diversification_labels": page_data.get("diversification_labels") or [],
        "location_created": page_data.get("location_created"),
        "author": info.get("uploader") or info.get("creator"),
        "author_id": info.get("uploader_id"),
        "author_url": info.get("uploader_url"),
        "timestamp": info.get("timestamp"),
        "upload_date": info.get("upload_date"),
        "duration_sec": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "thumbnail": info.get("thumbnail"),
        "video_url": info.get("url"),
        "webpage_url": info.get("webpage_url"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument(
        "--places",
        action="store_true",
        help="Run LLM extraction to surface specific places mentioned",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    try:
        result = extract(args.url)
    except DownloadError as e:
        print(json.dumps({"error": "extraction_failed", "detail": str(e)}, indent=2))
        return 1

    if args.places:
        from places import extract_places

        places_started = time.perf_counter()
        result["places"] = extract_places(
            caption=result["caption"],
            transcript=result["transcript"],
            hashtags=result["hashtags"],
            stickers=result["stickers"],
            suggested_words=result["suggested_words"],
            location_created=result["location_created"],
        )
        result["_places_ms"] = int((time.perf_counter() - places_started) * 1000)

    result["_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

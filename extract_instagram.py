"""Extract caption + metadata from an Instagram Reel or Post URL via yt-dlp.

Instagram doesn't expose subtitle tracks like TikTok does, so v1 is
caption-only. Place extraction reuses places.extract_places() — same
Claude prompt, just with no transcript signal.
"""

import re

from yt_dlp import YoutubeDL

import net


INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def is_instagram_url(url: str) -> bool:
    return bool(INSTAGRAM_URL_RE.match(url or ""))


def resolve_redirects(url: str) -> str:
    try:
        with net.urlopen(url, method="HEAD", timeout=5) as resp:
            return resp.geturl()
    except Exception:
        return url


def extract_instagram(url: str) -> dict:
    canonical = resolve_redirects(url)
    opts = net.ydl_opts()
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(canonical, download=False)

    description = info.get("description") or ""
    hashtags = re.findall(r"#(\w+)", description)

    return {
        "input_url": url,
        "canonical_url": info.get("webpage_url") or canonical,
        "id": info.get("id"),
        "caption": description,
        "hashtags": hashtags,
        "transcript": None,
        "has_subtitles": False,
        "stickers": [],
        "suggested_words": [],
        "diversification_labels": [],
        "location_created": None,
        "author": info.get("uploader") or info.get("creator") or info.get("channel"),
        "author_id": info.get("uploader_id"),
        "author_url": info.get("uploader_url") or info.get("channel_url"),
        "timestamp": info.get("timestamp"),
        "upload_date": info.get("upload_date"),
        "duration_sec": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
    }

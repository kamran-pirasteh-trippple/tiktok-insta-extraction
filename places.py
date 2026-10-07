"""Extract specific real-world places from caption + transcript using Claude."""

import os
import threading
from typing import Literal, Optional

from anthropic import Anthropic
from pydantic import BaseModel, Field

MODEL = "claude-haiku-4-5"

_client: Optional[Anthropic] = None
_client_lock = threading.Lock()


def get_client() -> Anthropic:
    """One Anthropic client for the whole process.

    This used to be constructed inside every call, so each import paid a fresh
    TLS handshake and got no connection reuse. The explicit timeout matters
    under load: without it a stalled Claude call holds its concurrency slot
    indefinitely.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = Anthropic(
                    timeout=float(os.environ.get("EXTRACT_LLM_TIMEOUT_S") or 30),
                    max_retries=2,
                )
    return _client


def ping() -> None:
    """Cheapest call that still proves the API key is accepted.

    Called once at startup so a bad/rotated key surfaces on /health instead of
    as an opaque 500 on every import.
    """
    get_client().messages.create(
        model=MODEL,
        max_tokens=1,
        messages=[{"role": "user", "content": "."}],
    )


class Place(BaseModel):
    name: str = Field(description="Cleaned, canonical name of the place")
    type: Literal[
        "restaurant",
        "bar",
        "cafe",
        "park",
        "museum",
        "neighborhood",
        "transit",
        "shop",
        "hotel",
        "landmark",
        "other",
    ]
    city: Optional[str] = None
    country: Optional[str] = None
    context: str = Field(
        description="Short snippet (≤25 words) describing what was said about this place"
    )


class PlacesResponse(BaseModel):
    places: list[Place]


SYSTEM = """You extract real-world places from social-media video content. You receive several signals: caption, hashtags, on-screen-text stickers (creator-typed — usually the cleanest place names), audio transcript (often noisy auto-transcription), and TikTok-suggested keywords.

Rules:
- Trust stickers as ground truth for spelling when they conflict with the transcript.
- Only extract specific, named places: restaurants, bars, cafes, parks, museums, neighborhoods, transit lines, shops, hotels, landmarks. Skip generic descriptors ("a cafe", "the park").
- Fix transcription errors using stickers/hashtags/context. Examples: "Boydeven Sen" → "Bois de Vincennes"; "Pomonette" → "Promenade Plantée".
- Use the most canonical, searchable form of the name.
- Infer city/country from context (hashtags, suggested keywords, location_created).
- Skip the city/country itself unless it's the only thing mentioned. We want specific spots, not the destination.
- Keep `context` short — what was said about it, not a full quote."""


def extract_places(
    caption: str,
    transcript: Optional[str],
    hashtags: list[str],
    stickers: Optional[list[str]] = None,
    suggested_words: Optional[list[str]] = None,
    location_created: Optional[str] = None,
) -> list[dict]:
    parts = [
        f"Caption: {caption}",
        f"Hashtags: {', '.join(hashtags) if hashtags else '(none)'}",
        f"On-screen stickers (ground truth): {' | '.join(stickers) if stickers else '(none)'}",
        f"Suggested keywords: {', '.join(suggested_words) if suggested_words else '(none)'}",
        f"Location created: {location_created or '(unknown)'}",
        "",
        f"Transcript:\n{transcript or '(no transcript)'}",
    ]
    user = "\n".join(parts)

    response = get_client().messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=PlacesResponse,
    )

    parsed = response.parsed_output
    if parsed is None:
        return []
    return [p.model_dump() for p in parsed.places]

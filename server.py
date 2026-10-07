"""FastAPI wrapper around extract.py / places.py for the trippple-react import flow."""

import asyncio
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

import anyio
import anyio.to_thread
from anthropic import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    RateLimitError,
)
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from yt_dlp.utils import DownloadError

import net
import places
from cache import ResultCache
from extract import extract, fetch_comments
from extract_instagram import extract_instagram
from places import extract_places

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("extraction")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-integer %s=%r; using %d", name, raw, default)
        return default


# An extraction has two phases with completely different constraints, so they
# get separate limiters. Measured against prod (2026-07-13, warm):
#
#     yt-dlp + page scrape + 50 comments  ~1.2s   15% of the wall clock
#     Claude (extract_places)             ~6.5s   85% of the wall clock
#
# Gating both behind one limiter throttles the wrong thing. The platform calls
# are what get a datacenter IP blocked, and they're the short phase; Anthropic
# tolerates far more concurrency than TikTok does, and it's where the time
# actually goes. One shared limiter of N would cap throughput at N/7.5 req/s
# while leaving TikTok exposure unchanged — strictly worse on both axes.

# Concurrent requests allowed to touch TikTok/Instagram. Deliberately small:
# this is the number that decides whether our IP gets blocked. (FastAPI would
# otherwise run sync endpoints on a 40-thread pool — 40 simultaneous anonymous
# hits from one IP, which is how we got blocked in the first place.)
SCRAPE_CONCURRENCY = _env_int("EXTRACT_SCRAPE_CONCURRENCY", 6)

# Concurrent Claude calls. Bounded by Anthropic's rate limits, not by any IP
# reputation, so it can be much higher. This is the real throughput lever.
LLM_CONCURRENCY = _env_int("EXTRACT_LLM_CONCURRENCY", 24)

# Total requests admitted (running + queued) before we start shedding.
#
# Sized against the deadline, not plucked from air. At ~3.7 req/s sustained
# (min of 6/1.2s scrape and 24/6.5s LLM), 96 requests drain in ~26s, so even
# the last one answers inside DEADLINE_S. Admitting more than we can finish is
# worse than a rejection: the app gives up at 45s, but a thread inside yt-dlp
# cannot be cancelled, so we'd keep paying for work nobody will read — and the
# modal's "Try Again" button then piles more on top.
MAX_INFLIGHT = _env_int("EXTRACT_MAX_INFLIGHT", 96)

# Answer before the app's 45s client timeout so it sees a real status code
# rather than an aborted socket.
DEADLINE_S = _env_int("EXTRACT_DEADLINE_S", 40)

CACHE_TTL_S = _env_int("EXTRACT_CACHE_TTL_S", 3600)
CACHE_MAX_ENTRIES = _env_int("EXTRACT_CACHE_MAX_ENTRIES", 500)

# Opt-in shared-key gate, matching services/extractAuth.ts on the app side.
# Unset (the default) leaves the endpoints open — which is how they run today.
EXTRACT_API_KEY = os.environ.get("EXTRACT_API_KEY") or ""

_scrape_limiter = anyio.CapacityLimiter(SCRAPE_CONCURRENCY)
_llm_limiter = anyio.CapacityLimiter(LLM_CONCURRENCY)
_cache = ResultCache(ttl_s=CACHE_TTL_S, max_entries=CACHE_MAX_ENTRIES)
_inflight = 0
_llm_status = "unchecked"

# Upstream telling us it is refusing *this IP*, as opposed to the post being
# deleted or private. Worth its own status so the two don't blur together in
# logs — one means "buy a proxy", the other means "nothing to do".
_BLOCKED_RE = re.compile(
    r"IP address is blocked|empty media response|rate.?limit|login required|"
    r"sign in to confirm|not available in your (country|region)",
    re.IGNORECASE,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate the Anthropic key once at boot.

    Every import runs a Claude call server-side, so a missing or rotated key
    breaks the whole service — previously as an opaque 500 per request, with
    nothing in /health to say why. One cheap call at startup turns that into a
    loud log line and a visible health field.
    """
    global _llm_status
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _llm_status = "missing_key"
        log.critical(
            "ANTHROPIC_API_KEY is not set — every extraction will fail at the places step"
        )
    else:
        try:
            await anyio.to_thread.run_sync(places.ping)
            _llm_status = "ok"
            log.info("anthropic key validated (model=%s)", places.MODEL)
        except AuthenticationError:
            _llm_status = "auth_failed"
            log.critical(
                "ANTHROPIC_API_KEY was rejected (401) — every extraction will fail at "
                "the places step. Has the key been rotated without updating this service?"
            )
        except Exception as exc:  # network hiccup at boot shouldn't crashloop us
            _llm_status = "unreachable"
            log.error("could not validate ANTHROPIC_API_KEY: %s", exc)

    log.info(
        "extraction service up: scrape_concurrency=%d llm_concurrency=%d "
        "max_inflight=%d deadline=%ds cache_ttl=%ds auth_gate=%s proxy=%s cookies=%s",
        SCRAPE_CONCURRENCY,
        LLM_CONCURRENCY,
        MAX_INFLIGHT,
        DEADLINE_S,
        CACHE_TTL_S,
        bool(EXTRACT_API_KEY),
        bool(net.proxy_url()),
        bool(net.cookie_file()),
    )
    yield


app = FastAPI(title="trippple tiktok extraction", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://localhost:19006",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)(:\d+)?$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    url: str
    city_hint: Optional[str] = None  # accepted for app compatibility; unused today


def _require_key(provided: Optional[str]) -> None:
    if not EXTRACT_API_KEY:
        return
    if not provided or not secrets.compare_digest(provided, EXTRACT_API_KEY):
        raise HTTPException(status_code=401, detail="invalid_extract_key")


def _http_error_for(exc: BaseException) -> HTTPException:
    """Map an internal failure onto a status the client can act on.

    Previously only `extract()` was wrapped, so anything that went wrong in
    fetch_comments/extract_places escaped as a bare text 500 with no clue as to
    which of the three upstreams (TikTok, Instagram, Anthropic) had failed.
    """
    if isinstance(exc, DownloadError):
        message = str(exc)
        if _BLOCKED_RE.search(message):
            return HTTPException(502, detail=f"upstream_blocked: {message}")
        return HTTPException(502, detail=f"extraction_failed: {message}")
    if isinstance(exc, AuthenticationError):
        return HTTPException(503, detail="llm_auth_failed: ANTHROPIC_API_KEY rejected")
    if isinstance(exc, RateLimitError):
        return HTTPException(
            429, detail="llm_rate_limited", headers={"Retry-After": "10"}
        )
    if isinstance(exc, (APIConnectionError, APIStatusError)):
        return HTTPException(502, detail=f"llm_failed: {exc}")
    return HTTPException(500, detail=f"unexpected: {type(exc).__name__}: {exc}")


async def _await_with_deadline(awaitable, cache_key: str):
    try:
        with anyio.fail_after(DEADLINE_S):
            return await awaitable
    except TimeoutError:
        raise HTTPException(
            504, detail=f"timeout: no result within {DEADLINE_S}s"
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("extraction failed: %s", cache_key)
        raise _http_error_for(exc) from exc


async def _guarded(cache_key: str, pipeline: Callable[[], "Awaitable[dict]"]) -> dict:
    """Admission control + coalescing + deadline around one blocking extraction."""
    global _inflight

    # Served straight from cache: no slot, no queue, never shed.
    cached = _cache.peek(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}

    # Someone is already extracting this exact URL. Join them. This costs no
    # thread and no upstream slot, so it must skip admission control entirely:
    # a hundred people importing the same viral post is the case we most want
    # to serve, not the one we shed. `shield` keeps our own deadline (or a
    # client hang-up) from cancelling the extraction everyone else is awaiting.
    existing = _cache.inflight(cache_key)
    if existing is not None:
        value = await _await_with_deadline(asyncio.shield(existing), cache_key)
        return {**value, "_cache": "coalesced"}

    # From here on we are doing real upstream work, so we are admission-controlled.
    if _inflight >= MAX_INFLIGHT:
        log.warning("shedding request (%d in flight): %s", _inflight, cache_key)
        raise HTTPException(503, detail="overloaded", headers={"Retry-After": "5"})

    _inflight += 1
    try:

        value = await _await_with_deadline(
            _cache.produce(cache_key, pipeline), cache_key
        )
        return {**value, "_cache": "miss"}
    finally:
        _inflight -= 1


async def _scrape(fn, *args):
    """Platform-facing phase. Tight limiter: this is what gets our IP blocked."""
    return await anyio.to_thread.run_sync(
        lambda: fn(*args), limiter=_scrape_limiter, abandon_on_cancel=True
    )


async def _llm(fn, **kwargs):
    """Anthropic-facing phase. Looser limiter: bounded by rate limits, not IP reputation."""
    return await anyio.to_thread.run_sync(
        lambda: fn(**kwargs), limiter=_llm_limiter, abandon_on_cancel=True
    )


def _tiktok_scrape(url: str) -> tuple[dict, list[dict]]:
    info = extract(url)

    # Always pull top comments and merge into the LLM input. Captions often
    # under-specify (creator names 2 spots in the caption but rattles off 10
    # in the video / comments thread). Comments are unauthenticated and free
    # to fetch, so the cost is one HTTP call + a slightly fatter LLM prompt.
    comments = fetch_comments(
        aweme_id=info.get("id") or "",
        referer=info.get("canonical_url") or url,
        count=50,
    )
    existing_transcript = info.get("transcript") or ""
    if comments:
        comment_text = "\n".join(
            f"({c['digg_count']} likes) {c['text']}" for c in comments[:50]
        )
        merged_transcript = (
            f"{existing_transcript}\n\n--- top comments ---\n{comment_text}"
            if existing_transcript
            else f"--- top comments ---\n{comment_text}"
        )
    else:
        merged_transcript = existing_transcript or None

    info["_merged_transcript"] = merged_transcript
    return info, comments


async def _tiktok_pipeline(url: str) -> dict:
    started = time.perf_counter()
    info, comments = await _scrape(_tiktok_scrape, url)

    places_started = time.perf_counter()
    found = await _llm(
        extract_places,
        caption=info["caption"],
        transcript=info.get("_merged_transcript"),
        hashtags=info.get("hashtags") or [],
        stickers=info.get("stickers") or [],
        suggested_words=info.get("suggested_words") or [],
        location_created=info.get("location_created"),
    )
    places_ms = int((time.perf_counter() - places_started) * 1000)

    suggested_city = next((p["city"] for p in found if p.get("city")), None)
    if not suggested_city and info.get("location_created"):
        suggested_city = info["location_created"]

    return {
        "source": "tiktok",
        "places": found,
        "places_source": "caption_plus_comments" if comments else "caption_only",
        "suggested_city": suggested_city,
        "thumbnail": info.get("thumbnail"),
        "transcript": info.get("transcript"),
        "caption": info.get("caption"),
        "author": info.get("author"),
        "canonical_url": info.get("canonical_url"),
        "_places_ms": places_ms,
        "_comments_used": len(comments),
        "_elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def _instagram_pipeline(url: str) -> dict:
    started = time.perf_counter()
    info = await _scrape(extract_instagram, url)

    places_started = time.perf_counter()
    found = await _llm(
        extract_places,
        caption=info["caption"],
        transcript=None,
        hashtags=info.get("hashtags") or [],
        stickers=[],
        suggested_words=[],
        location_created=None,
    )
    places_ms = int((time.perf_counter() - places_started) * 1000)

    return {
        "source": "instagram",
        "places": found,
        "places_source": "primary",
        "suggested_city": next((p["city"] for p in found if p.get("city")), None),
        "thumbnail": info.get("thumbnail"),
        "transcript": None,
        "caption": info.get("caption"),
        "author": info.get("author"),
        "canonical_url": info.get("canonical_url"),
        "_places_ms": places_ms,
        "_elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": _llm_status == "ok",
        "llm": _llm_status,
        "inflight": _inflight,
        "capacity": {
            "scrape_concurrency": SCRAPE_CONCURRENCY,
            "llm_concurrency": LLM_CONCURRENCY,
            "max_inflight": MAX_INFLIGHT,
        },
        "cache": _cache.stats(),
        "proxy": bool(net.proxy_url()),
        "cookies": bool(net.cookie_file()),
    }


@app.post("/tiktok/extract")
async def tiktok_extract(
    req: ExtractRequest,
    x_extract_key: Optional[str] = Header(default=None),
) -> dict:
    _require_key(x_extract_key)
    return await _guarded(f"tiktok:{req.url}", lambda: _tiktok_pipeline(req.url))


@app.post("/instagram/extract")
async def instagram_extract(
    req: ExtractRequest,
    x_extract_key: Optional[str] = Header(default=None),
) -> dict:
    _require_key(x_extract_key)
    return await _guarded(f"instagram:{req.url}", lambda: _instagram_pipeline(req.url))

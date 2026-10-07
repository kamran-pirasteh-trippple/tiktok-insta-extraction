"""In-process load test for the concurrency guards.

Stubs the network and Claude, then drives the ASGI app directly, so we can
assert on behaviour under a burst instead of guessing at it — and without
sending a single request to TikTok.

Phase durations mirror what prod actually does (measured 2026-07-13, warm):
scrape ~1.2s, Claude ~6.5s. Scaled down 10x here to keep the test quick; what
matters is the ratio, since that's what makes the split limiters worth having.

    uv run python loadtest.py
"""

import asyncio
import os
import threading
import time

# Must be set before `server` is imported — config is read at module load.
os.environ.setdefault("EXTRACT_SCRAPE_CONCURRENCY", "6")
os.environ.setdefault("EXTRACT_LLM_CONCURRENCY", "24")
os.environ.setdefault("EXTRACT_MAX_INFLIGHT", "96")
os.environ.setdefault("EXTRACT_DEADLINE_S", "40")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-for-loadtest")

import httpx  # noqa: E402
from anthropic import AuthenticationError  # noqa: E402
from yt_dlp.utils import DownloadError  # noqa: E402

import server  # noqa: E402

SCRAPE_S = 0.12  # prod ~1.2s
LLM_S = 0.65     # prod ~6.5s

_lock = threading.Lock()
scrape_live = scrape_peak = scrape_calls = 0
llm_live = llm_peak = llm_calls = 0


def reset() -> None:
    global scrape_live, scrape_peak, scrape_calls, llm_live, llm_peak, llm_calls
    scrape_live = scrape_peak = scrape_calls = 0
    llm_live = llm_peak = llm_calls = 0
    server._cache._entries.clear()
    server._cache._inflight.clear()


def _fake_extract(url: str) -> dict:
    global scrape_live, scrape_peak, scrape_calls
    with _lock:
        scrape_live += 1
        scrape_peak = max(scrape_peak, scrape_live)
        scrape_calls += 1
    try:
        time.sleep(SCRAPE_S)
    finally:
        with _lock:
            scrape_live -= 1
    return {
        "id": "1", "caption": "cool spots", "hashtags": [], "transcript": None,
        "stickers": [], "suggested_words": [], "location_created": "Paris",
        "canonical_url": url, "thumbnail": None, "author": "someone",
    }


def _fake_places(**_kw) -> list[dict]:
    global llm_live, llm_peak, llm_calls
    with _lock:
        llm_live += 1
        llm_peak = max(llm_peak, llm_live)
        llm_calls += 1
    try:
        time.sleep(LLM_S)
    finally:
        with _lock:
            llm_live -= 1
    return [{"name": "Lilia", "type": "restaurant", "city": "Paris",
             "country": "France", "context": "pasta"}]


def install_stubs() -> None:
    server.extract = _fake_extract
    server.fetch_comments = lambda **_kw: []
    server.extract_places = _fake_places


async def burst(client, urls):
    return await asyncio.gather(
        *(client.post("/tiktok/extract", json={"url": u}) for u in urls)
    )


def codes(responses) -> dict:
    out: dict[int, int] = {}
    for r in responses:
        out[r.status_code] = out.get(r.status_code, 0) + 1
    return dict(sorted(out.items()))


async def main() -> int:
    install_stubs()
    failures = []
    transport = httpx.ASGITransport(app=server.app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=120
    ) as client:

        # 1. Viral post: 100 people import the SAME url at once.
        reset()
        t0 = time.perf_counter()
        got = codes(await burst(client, ["https://tiktok.com/@a/video/1"] * 100))
        print(f"\n[1] 100 concurrent, SAME url  -> {got} in {time.perf_counter()-t0:.1f}s")
        print(f"    scrapes: {scrape_calls}   claude calls: {llm_calls}")
        if (scrape_calls, llm_calls) != (1, 1):
            failures.append(f"same-url burst did {scrape_calls} scrapes / {llm_calls} llm, want 1/1")
        if got.get(200) != 100:
            failures.append(f"same-url burst: only {got.get(200)}/100 succeeded")

        # 2. 100 DISTINCT urls — the genuine worst case.
        reset()
        t0 = time.perf_counter()
        got = codes(await burst(client, [f"https://tiktok.com/@a/video/{i}" for i in range(100)]))
        elapsed = time.perf_counter() - t0
        print(f"\n[2] 100 concurrent, DISTINCT  -> {got} in {elapsed:.1f}s")
        print(f"    peak scrape concurrency: {scrape_peak}  (cap {server.SCRAPE_CONCURRENCY})")
        print(f"    peak claude concurrency: {llm_peak}  (cap {server.LLM_CONCURRENCY})")
        if scrape_peak > server.SCRAPE_CONCURRENCY:
            failures.append(f"scrape peak {scrape_peak} > cap {server.SCRAPE_CONCURRENCY}")
        if llm_peak > server.LLM_CONCURRENCY:
            failures.append(f"llm peak {llm_peak} > cap {server.LLM_CONCURRENCY}")
        if got.get(500):
            failures.append(f"{got[500]} bare 500s under load")
        if got.get(200, 0) + got.get(503, 0) != 100:
            failures.append("some requests got no definite answer")
        # Scaled 10x, so this stands in for a real ~40s deadline.
        if elapsed > 4.0:
            failures.append(f"100 distinct took {elapsed:.1f}s (scaled) — tail would miss the deadline")

        # 3. Rotated/dead Anthropic key must be a typed 503, not a bare 500.
        reset()
        def _auth_fail(**_kw):
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise AuthenticationError(
                "invalid x-api-key",
                response=httpx.Response(401, request=request), body=None,
            )
        server.extract_places = _auth_fail
        r = await client.post("/tiktok/extract", json={"url": "https://tiktok.com/@a/video/x"})
        print(f"\n[3] dead Anthropic key        -> {r.status_code} {r.json().get('detail')}")
        if r.status_code != 503:
            failures.append(f"dead key gave {r.status_code}, want 503")

        # 4. Platform blocking our IP must be distinguishable from a dead post.
        reset()
        server.extract_places = _fake_places
        def _blocked(url: str):
            raise DownloadError(
                "ERROR: [TikTok] 123: Your IP address is blocked from accessing this post"
            )
        server.extract = _blocked
        r = await client.post("/tiktok/extract", json={"url": "https://tiktok.com/@a/video/y"})
        detail = r.json().get("detail", "")
        print(f"\n[4] upstream IP block         -> {r.status_code} {detail[:58]}")
        if r.status_code != 502 or not detail.startswith("upstream_blocked"):
            failures.append(f"IP block gave {r.status_code} {detail[:40]!r}")

    print()
    for f in failures:
        print(f"FAIL: {f}")
    if not failures:
        print("all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

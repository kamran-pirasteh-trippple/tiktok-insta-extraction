# tiktok-insta-extraction

Phase 1 experiment: paste a TikTok URL, get caption + metadata as JSON.

## Setup

```bash
uv sync
```

## Usage

```bash
uv run python extract.py "https://www.tiktok.com/@user/video/1234567890"
```

Or with a short link:

```bash
uv run python extract.py "https://vm.tiktok.com/abc123/"
```

## Sample output

```json
{
  "input_url": "https://vm.tiktok.com/abc123/",
  "canonical_url": "https://www.tiktok.com/@user/video/1234567890",
  "id": "1234567890",
  "caption": "best pasta in nyc 🍝 Lilia in Williamsburg is insane #foodtok",
  "hashtags": ["foodtok"],
  "author": "user",
  "timestamp": 1700000000,
  "duration_sec": 22,
  "view_count": 1234567,
  "like_count": 89000,
  "thumbnail": "https://...",
  "video_url": "https://...",
  "_elapsed_ms": 850
}
```

## Local API server

For use from the trippple-react Import Itinerary modal:

```bash
doppler run --project trippple-recs-engine --config dev --command \
  'ANTHROPIC_API_KEY="$CLAUDE_HAIKU" uv run uvicorn server:app --host 0.0.0.0 --port 8765 --reload'
```

Smoke test:

```bash
curl -s -X POST localhost:8765/tiktok/extract \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.tiktok.com/@sagevanalstine/video/7236850924650548523"}' | jq
```

The mobile app reaches this from a phone over LAN — set
`EXPO_PUBLIC_TIKTOK_API_URL=http://<mac-lan-ip>:8765` in `trippple-react/.env.local`.

## Configuration

All optional except `ANTHROPIC_API_KEY`.

| Env var | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | **Required.** Every import runs a Claude call server-side. Validated once at boot; if it's missing or rejected, `/health` reports `llm: auth_failed` and logs CRITICAL. |
| `EXTRACT_SCRAPE_CONCURRENCY` | `6` | Concurrent requests allowed to touch TikTok/Instagram. This is the number that decides whether our IP gets blocked. |
| `EXTRACT_LLM_CONCURRENCY` | `24` | Concurrent Claude calls. Bounded by Anthropic rate limits, not IP reputation — so it can be much higher. |
| `EXTRACT_MAX_INFLIGHT` | `96` | Total requests admitted (running + queued) before we shed with `503` + `Retry-After`. |
| `EXTRACT_DEADLINE_S` | `40` | Per-request ceiling. Kept under the app's 45s client timeout. |
| `EXTRACT_CACHE_TTL_S` | `3600` | How long an extracted result is reused for the same URL. |
| `EXTRACT_API_KEY` | unset | Opt-in shared-key gate. When set, requests must send a matching `X-Extract-Key`. Pairs with `EXPO_PUBLIC_EXTRACT_KEY` in the app. Unset = endpoints are open. |
| `EXTRACT_PROXY_URL` | unset | Proxy for **all** upstream calls (yt-dlp *and* the raw urllib page/comment/subtitle fetches). |
| `EXTRACT_COOKIES_FILE` | unset | Netscape-format cookie jar. Instagram needs one for most posts. |

### Tuning the concurrency limits

An extraction has two phases with completely different constraints. Measured
against prod (2026-07-13, warm, 50 comments merged):

| Phase | Time | Share | Constrained by |
| --- | --- | --- | --- |
| yt-dlp + page scrape + comments | ~1.2s | 15% | **TikTok/Instagram IP reputation** |
| Claude (`extract_places`) | ~6.5s | 85% | Anthropic rate limits |

So they get **separate limiters**. Gating both behind one number throttles the
wrong thing: the platform calls are what get a datacenter IP blocked, and
they're the *short* phase, while Anthropic — which tolerates far more
concurrency than TikTok does — is where the wall-clock actually goes.

The difference is not marginal. Against a burst of 100 distinct URLs:

| | served | shed | peak TikTok concurrency |
| --- | --- | --- | --- |
| one limiter of 6 | 30 | 70 | 6 |
| split 6 scrape / 24 LLM | **96** | 4 | **6** |

Same IP exposure, 3x the throughput.

`MAX_INFLIGHT` is sized against `DEADLINE_S`, not picked by feel: at ~3.7 req/s
sustained (the min of 6/1.2s and 24/6.5s), 96 requests drain in ~26s, so even
the last one answers inside the 40s deadline. Raising it past what we can finish
is worse than a rejection — the app gives up at 45s, but a thread inside yt-dlp
cannot be cancelled, so we'd keep paying for work nobody will read.

**To serve more TikTok traffic, add upstream capacity (proxies) — not queue
depth.** `EXTRACT_SCRAPE_CONCURRENCY` is the one number you should not raise
without one.

### Load test

Exercises the guards against stubbed upstreams — no network, no TikTok:

```bash
uv run python loadtest.py
```

Asserts that a same-URL burst coalesces to one extraction, that concurrency
never exceeds the cap, that overflow sheds with `503` rather than hanging, and
that a rotated Anthropic key surfaces as `503 llm_auth_failed` rather than an
opaque `500`.

## What's next

- Cookies + rotating/residential proxy for TikTok and Instagram (issue #3) —
  the service is currently IP-blocked by TikTok and cannot read most Instagram
  posts. Plumbing is in place (`EXTRACT_PROXY_URL`, `EXTRACT_COOKIES_FILE`);
  it needs credentials.
- Shared result cache (Redis) if this ever runs on more than one instance — the
  current cache is per-process.

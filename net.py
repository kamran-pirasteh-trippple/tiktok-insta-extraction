"""Shared outbound-network config for every upstream call we make.

Every request that touches TikTok or Instagram goes through here. That matters
for proxying: yt-dlp is only *half* our traffic. Resolving the short link,
scraping the page for stickers, fetching the comment list and pulling the
subtitle track are all raw urllib. Setting a proxy on yt-dlp alone would still
expose the origin IP on those four, which defeats the point of having one.
"""

import os
import urllib.request
from typing import Optional

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SOCKET_TIMEOUT_S = int(os.environ.get("EXTRACT_SOCKET_TIMEOUT_S") or 15)


def proxy_url() -> str:
    """Residential/rotating proxy, if configured. See issue #3."""
    return os.environ.get("EXTRACT_PROXY_URL") or ""


def cookie_file() -> str:
    """Netscape-format cookie jar. Instagram needs one for most posts."""
    path = os.environ.get("EXTRACT_COOKIES_FILE") or ""
    return path if path and os.path.isfile(path) else ""


def _build_opener() -> urllib.request.OpenerDirector:
    proxy = proxy_url()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


_OPENER = _build_opener()


def urlopen(
    url: str,
    *,
    method: str = "GET",
    referer: Optional[str] = None,
    timeout: Optional[int] = None,
):
    headers = {"User-Agent": BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, method=method, headers=headers)
    return _OPENER.open(req, timeout=timeout or SOCKET_TIMEOUT_S)


def ydl_opts(**extra) -> dict:
    """Base yt-dlp options, with proxy/cookies applied when configured."""
    opts = {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
        "socket_timeout": SOCKET_TIMEOUT_S,
        # Bound retries. yt-dlp's default retry loop can stall a request well
        # past the caller's deadline, holding a concurrency slot the whole time.
        "retries": 1,
        "extractor_retries": 1,
    }
    if proxy_url():
        opts["proxy"] = proxy_url()
    if cookie_file():
        opts["cookiefile"] = cookie_file()
    opts.update(extra)
    return opts

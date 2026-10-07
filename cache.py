"""TTL cache with single-flight coalescing for extraction results.

A viral post is precisely what produces a burst: many users importing the *same*
URL within the same minute. Without coalescing that is N yt-dlp hits (which is
how a datacenter IP earns a block) plus N Claude calls, for N identical answers.
With it, the first request does the work and everyone else waits on that result.

Callers are expected to check `peek()`, then `inflight()`, and only apply
admission control to what's left — a coalesced waiter holds no thread and no
upstream slot, so it must never be shed.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional


class ResultCache:
    def __init__(self, ttl_s: float, max_entries: int) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._entries: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    def peek(self, key: str) -> Optional[Any]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        self.hits += 1
        return value

    def inflight(self, key: str) -> Optional[asyncio.Future]:
        """The in-progress extraction for this key, if someone else is running it."""
        future = self._inflight.get(key)
        if future is not None:
            self.coalesced += 1
        return future

    def _store(self, key: str, value: Any) -> None:
        if len(self._entries) >= self._max:
            # Evict whatever expires soonest. At this size a full scan beats
            # maintaining an LRU.
            soonest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(soonest, None)
        self._entries[key] = (time.monotonic() + self._ttl, value)

    async def produce(self, key: str, producer: Callable[[], Awaitable[Any]]) -> Any:
        """Become the single flight for `key`, and publish the result to joiners.

        The future is registered synchronously, before the first await, so a
        concurrent caller checking `inflight()` cannot slip past and start a
        second extraction for the same key.
        """
        self.misses += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await producer()
        except BaseException as exc:
            self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved, so a failure with no joiners doesn't surface
                # as an "exception never retrieved" warning.
                future.exception()
            raise
        else:
            self._store(key, value)
            self._inflight.pop(key, None)
            if not future.done():
                future.set_result(value)
            return value

    def stats(self) -> dict:
        return {
            "entries": len(self._entries),
            "inflight": len(self._inflight),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
        }

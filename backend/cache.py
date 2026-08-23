"""In-process TTL cache with a bound.

Single-process and in-memory, deliberately: there is one uvicorn worker
and no Redis on this box.

It used to be a plain dict, which leaked. Expiry was only ever checked on
`get`, so an entry nobody read again was never freed — and the body sweep
writes hundreds of `reader:<url>` entries (full article text) every 25
minutes that no request ever reads. Under load the process grew from
71 MB to 154 MB on a 416 MB box with ~390 MB already in swap.

So: capped entry count with LRU eviction, plus a cheap opportunistic
purge of expired keys on write. `_store` stays an OrderedDict because
several call sites treat it as a dict (`len`, `.keys()`, `.pop`).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

# Roughly 40-60 MB of payloads at observed entry sizes, which leaves
# headroom on a 416 MB box. Raise it if the box gets more RAM.
MAX_ENTRIES = 2000

# How many keys to inspect for expiry per write. Full scans on every set
# would be O(n) per request; this amortises the cleanup instead.
_PURGE_SCAN = 40

_store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_evictions = 0
_expired = 0


def get(key: str) -> Any | None:
    entry = _store.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        _store.pop(key, None)
        return None
    _store.move_to_end(key)          # LRU: reading counts as recent use
    return value


def _purge_some(now: float) -> None:
    """Drop expired keys from the oldest end. Cheap and bounded."""
    global _expired
    for key in list(_store.keys())[:_PURGE_SCAN]:
        entry = _store.get(key)
        if entry and now > entry[0]:
            _store.pop(key, None)
            _expired += 1


def set(key: str, value: Any, ttl_seconds: int) -> None:
    global _evictions
    now = time.time()
    _store[key] = (now + ttl_seconds, value)
    _store.move_to_end(key)
    if len(_store) > MAX_ENTRIES:
        _purge_some(now)
        # Still over after purging → evict least-recently-used.
        while len(_store) > MAX_ENTRIES:
            _store.popitem(last=False)
            _evictions += 1


def delete(key: str) -> bool:
    """Forget one key. Used when a cached payload turns out to be
    unservable, so the next request re-derives instead of re-serving it."""
    return _store.pop(key, None) is not None


def clear() -> None:
    _store.clear()


def stats() -> dict:
    return {
        "entries": len(_store),
        "max_entries": MAX_ENTRIES,
        "evictions": _evictions,
        "expired_purged": _expired,
    }

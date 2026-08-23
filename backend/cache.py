"""In-process TTL cache with a byte budget.

Single-process and in-memory, deliberately: there is one uvicorn worker
and no Redis on this box.

History, because both mistakes are easy to repeat:

1. It was a plain dict whose expiry was only checked on read, so entries
   nobody read again were never freed. The body sweep writes hundreds of
   `reader:<url>` values (full article text) every 25 minutes that no
   request ever reads, so the process grew unbounded.

2. Capping the *entry count* did not fix it. Values here range from a
   40-byte resolved URL to a 60 KB article body, so 2,000 entries could
   still mean 150 MB. On a 416 MB box that meant swap thrash: p50 stayed
   fine while p95 blew out to 5-8 s, the signature of paging rather than
   saturation.

So the budget is in bytes, with LRU eviction. A few keys are pinned:
they are expensive to rebuild (`brief:response` costs a full curator
pass) and evicting one turns a cache miss into a multi-second stall for
every concurrent reader.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from itertools import islice
from typing import Any

# ~35 MB of payloads, chosen against a 416 MB box that idles at ~70 MB
# RSS. Raise it if the instance gets more RAM.
MAX_BYTES = 35 * 1024 * 1024
# Backstop so pathological numbers of tiny keys can't bloat the dict.
MAX_ENTRIES = 5000
# Expired keys inspected per write. Bounded so `set` stays O(1)-ish.
_PURGE_SCAN = 40

# Never evicted: costly to rebuild, and a miss stalls every reader at
# once. They still expire normally on TTL.
PINNED = ("brief:response", "rss:all")

_store: "OrderedDict[str, tuple[float, Any, int]]" = OrderedDict()
_bytes = 0
_evictions = 0
_expired = 0


def _approx_size(v: Any, depth: int = 0) -> int:
    """Rough byte cost of a cached value.

    Cheap and approximate on purpose — this runs on every write, so it
    walks only the shapes we actually store (str, bytes, list, dict,
    dataclass) and stops at depth 4 rather than being exhaustive.
    """
    if v is None:
        return 8
    if isinstance(v, str):
        return len(v) + 40
    if isinstance(v, (bytes, bytearray)):
        return len(v) + 30
    if isinstance(v, (int, float, bool)):
        return 28
    if depth >= 4:
        return 200
    if isinstance(v, dict):
        return 60 + sum(_approx_size(k, depth + 1) + _approx_size(x, depth + 1)
                        for k, x in list(v.items())[:200])
    if isinstance(v, (list, tuple, set)):
        return 60 + sum(_approx_size(x, depth + 1) for x in list(v)[:200])
    d = getattr(v, "__dict__", None)
    if d:
        return 60 + _approx_size(d, depth + 1)
    slots = getattr(v, "__slots__", None)
    if slots:
        return 60 + sum(_approx_size(getattr(v, s, None), depth + 1) for s in slots)
    return 200


def get(key: str) -> Any | None:
    entry = _store.get(key)
    if not entry:
        return None
    expires_at, value, _ = entry
    if time.time() > expires_at:
        _drop(key)
        return None
    _store.move_to_end(key)          # LRU: a read counts as recent use
    return value


def _drop(key: str) -> bool:
    global _bytes
    entry = _store.pop(key, None)
    if entry is None:
        return False
    _bytes -= entry[2]
    if _bytes < 0:
        _bytes = 0
    return True


def _purge_expired(now: float) -> None:
    global _expired
    # islice over the iterator, not list(keys()) — the latter copies the
    # whole key list on every write, which is the O(n) cost this is
    # meant to avoid.
    for key in list(islice(iter(_store), _PURGE_SCAN)):
        entry = _store.get(key)
        if entry and now > entry[0]:
            _drop(key)
            _expired += 1


def set(key: str, value: Any, ttl_seconds: int) -> None:
    global _bytes, _evictions
    now = time.time()
    size = _approx_size(value)
    _drop(key)                        # replacing: reclaim the old bytes
    _store[key] = (now + ttl_seconds, value, size)
    _store.move_to_end(key)
    _bytes += size

    if _bytes <= MAX_BYTES and len(_store) <= MAX_ENTRIES:
        return
    _purge_expired(now)
    # Still over → evict least-recently-used, skipping pinned keys.
    for k in list(_store.keys()):
        if _bytes <= MAX_BYTES and len(_store) <= MAX_ENTRIES:
            break
        if k == key or k in PINNED:
            continue
        if _drop(k):
            _evictions += 1


def delete(key: str) -> bool:
    """Forget one key. Used when a cached payload turns out to be
    unservable, so the next request re-derives instead of re-serving it."""
    return _drop(key)


def clear() -> None:
    global _bytes
    _store.clear()
    _bytes = 0


def stats() -> dict:
    return {
        "entries": len(_store),
        "bytes": _bytes,
        "mb": round(_bytes / 1024 / 1024, 1),
        "max_mb": round(MAX_BYTES / 1024 / 1024, 1),
        "evictions": _evictions,
        "expired_purged": _expired,
    }

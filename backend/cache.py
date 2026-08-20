"""Dumb TTL cache. Single-process, in-memory. Good enough for now."""

from __future__ import annotations

import time
from typing import Any


_store: dict[str, tuple[float, Any]] = {}


def get(key: str) -> Any | None:
    entry = _store.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        _store.pop(key, None)
        return None
    return value


def set(key: str, value: Any, ttl_seconds: int) -> None:
    _store[key] = (time.time() + ttl_seconds, value)


def delete(key: str) -> bool:
    """Forget one key. Used when a cached payload turns out to be
    unservable, so the next request re-derives instead of re-serving it."""
    return _store.pop(key, None) is not None


def clear() -> None:
    _store.clear()

"""Illustrator agent — fills in AI images for articles that arrive
without a photo. REAL article images are never touched.

The agent runs in the background on a 1-hour cycle:
  1. Refresh RSS (the existing cache-clear path)
  2. Walk the returned articles
  3. For each article whose .image is None, set .ai_image to a
     Pollinations.ai URL seeded by a hash of the title (stable —
     the same headline always resolves to the same picture).
  4. Optionally pre-warm those URLs so the browser hits them after
     Pollinations has already generated the image.

This is a free service (no key, no signup). URL generation itself
is just string construction — no network call needed to produce the
URL — but the warming step does fire HTTP requests so the images
are cached on Pollinations' CDN.
"""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import quote

import httpx


POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def ai_image_url(title: str, w: int = 800, h: int = 500) -> str | None:
    """Return a stable Pollinations URL for a title, or None if no title."""
    if not title:
        return None
    prompt = quote(title.strip()[:220])
    seed = int(hashlib.md5(title.encode("utf-8")).hexdigest()[:8], 16)
    return (
        f"{POLLINATIONS_BASE}/{prompt}"
        f"?width={w}&height={h}&nologo=true&seed={seed}&enhance=true"
    )


async def _warm_one(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.get(url, timeout=45.0)
        return r.status_code == 200
    except Exception:
        return False


async def warm_urls(urls: list[str], concurrency: int = 3) -> int:
    """Fire a controlled number of parallel GETs so Pollinations
    pre-generates + caches the images. Returns count of successful warms."""
    if not urls:
        return 0
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        async def _one(u: str) -> bool:
            async with sem:
                return await _warm_one(client, u)
        results = await asyncio.gather(*[_one(u) for u in urls])
    return sum(1 for r in results if r)

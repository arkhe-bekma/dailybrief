"""Reader agent — given an article URL, returns a clean extraction of
the main body, title, image, byline.

Powered by trafilatura, which is the go-to Python lib for this. Far
lighter than Hermes / scrapyd / chromium-based scrapers and works on
just about every news site we hit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict

import httpx
import trafilatura

from backend import cache


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class Reading:
    url: str
    title: str
    image: str | None
    byline: str | None
    text: str
    excerpt: str

    def to_dict(self) -> dict:
        return asdict(self)


async def extract(url: str) -> Reading | None:
    """Fetch the page and pull a clean Reading. None if extraction fails.

    Results are cached for 1 day per URL.
    """
    if not url:
        return None

    cache_key = f"reader:{url}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
            r = await client.get(url, follow_redirects=True, timeout=15.0)
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        print(f"[reader] fetch {url[:70]} failed: {exc}")
        return None

    data_json = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_images=True,
        include_links=False,
        favor_recall=True,
        url=url,
    )
    if not data_json:
        return None

    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return None

    reading = Reading(
        url=url,
        title=(data.get("title") or "").strip(),
        image=data.get("image"),
        byline=data.get("author"),
        text=(data.get("text") or data.get("raw_text") or "").strip(),
        excerpt=(data.get("description") or "").strip()[:300],
    )
    cache.set(cache_key, reading, 86400)
    return reading

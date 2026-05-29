"""RSS aggregator. Fetches each outlet in parallel and returns normalized items."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from backend import cache, config


@dataclass
class Article:
    title: str
    url: str
    outlet: str
    category: str
    summary: str
    published: str  # ISO 8601
    image: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_date(entry) -> str:
    # feedparser exposes parsed time tuples; fall back to raw 'published'.
    if getattr(entry, "published_parsed", None):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    if getattr(entry, "updated_parsed", None):
        dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _extract_image(entry) -> str | None:
    media = entry.get("media_content") or entry.get("media_thumbnail")
    if media and isinstance(media, list) and media:
        url = media[0].get("url")
        if url:
            return url
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]
    return None


def _parse_feed(raw_bytes: bytes, outlet: dict) -> list[Article]:
    parsed = feedparser.parse(raw_bytes)
    items: list[Article] = []
    for entry in parsed.entries[: config.PER_OUTLET_LIMIT]:
        items.append(Article(
            title=entry.get("title", "(no title)").strip(),
            url=entry.get("link", ""),
            outlet=outlet["name"],
            category=outlet["category"],
            summary=(entry.get("summary", "") or "").strip()[:400],
            published=_parse_date(entry),
            image=_extract_image(entry),
        ))
    return items


async def _fetch_one(client: httpx.AsyncClient, outlet: dict) -> list[Article]:
    try:
        r = await client.get(outlet["url"], timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        return _parse_feed(r.content, outlet)
    except Exception as exc:
        # Don't kill the whole batch for one bad feed.
        print(f"[rss] {outlet['name']} failed: {exc}")
        return []


async def fetch_all() -> list[Article]:
    cached = cache.get("rss:all")
    if cached is not None:
        return cached

    headers = {"User-Agent": "dailybrief/0.1 (+local)"}
    async with httpx.AsyncClient(headers=headers) as client:
        results = await asyncio.gather(*[_fetch_one(client, o) for o in config.OUTLETS])

    articles = [a for batch in results for a in batch]
    cache.set("rss:all", articles, config.FEED_CACHE_TTL)
    return articles

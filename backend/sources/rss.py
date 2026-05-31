"""RSS aggregator. Fetches each outlet in parallel and returns normalized items."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

import feedparser
import httpx
import trafilatura

from backend import cache, config
from backend.agent import illustrator


@dataclass
class Article:
    title: str
    url: str
    outlet: str
    category: str
    summary: str
    published: str  # ISO 8601
    lang: str = "en"
    image: str | None = None       # real RSS image, or AI-generated fallback

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


_IMG_TAG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property=["']og:image(?::secure_url)?["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_IMAGE_REV_RE = re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image(?::secure_url)?["']""",
    re.IGNORECASE,
)
_TWITTER_IMAGE_RE = re.compile(
    r"""<meta[^>]+name=["']twitter:image["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)


def _extract_image(entry) -> str | None:
    """First pass — look at every place an RSS feed can stash an image."""
    # 1. media:content / media:thumbnail
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media and isinstance(media, list) and media:
            url = media[0].get("url")
            if url:
                return url

    # 2. enclosure / link with image type
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/") and link.get("href"):
            return link["href"]

    # 3. itunes:image (some podcast-y feeds)
    itunes_img = entry.get("itunes_image") or entry.get("image")
    if isinstance(itunes_img, dict):
        href = itunes_img.get("href") or itunes_img.get("url")
        if href:
            return href

    # 4. First <img src="…"> inside any HTML content field
    for field in ("content", "summary_detail", "summary", "description"):
        val = entry.get(field)
        if isinstance(val, list) and val:
            val = val[0]
        if isinstance(val, dict):
            val = val.get("value", "")
        if isinstance(val, str) and val:
            m = _IMG_TAG_RE.search(val)
            if m:
                return unescape(m.group(1))

    return None


_OG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8,ko;q=0.6",
}


def _passes_reader_check(body: str, ctype: str, page_url: str) -> bool:
    """True only if trafilatura can actually pull a real-looking article
    out of `body`. Catches paywalls (NYT, WSJ), Google News interstitials,
    JS-only SPA shells, redirect pages, etc."""
    if not body or "html" not in (ctype or "").lower() or len(body) < 1024:
        return False
    try:
        text = trafilatura.extract(body, url=page_url, no_fallback=True)
    except Exception:
        return False
    # 60 words ≈ 2-3 sentences — paywalls usually return short blurbs or
    # nothing at all, interstitials return zero, real articles return 200+.
    return bool(text) and len(text.split()) >= 60


async def _fetch_og_image(client: httpx.AsyncClient, page_url: str) -> str | None:
    """Second pass — fetch the article page and pull og:image.

    Cached per URL so we don't re-scrape on every RSS refresh. Also
    records `reader_ok:{url}` based on whether trafilatura can pull a
    real body from the page, so fetch_all can drop dead/paywalled
    links before they reach the feed.
    """
    if not page_url:
        return None
    key = f"og_image:{page_url}"
    ok_key = f"reader_ok:{page_url}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # empty-string cache = known miss

    try:
        r = await client.get(page_url, timeout=8.0, follow_redirects=True, headers=_OG_HEADERS)
        if r.status_code >= 400:
            cache.set(key, "", 3600)
            cache.set(ok_key, False, 3600)
            return None
        ctype = r.headers.get("content-type", "").lower()
        body = r.text
        cache.set(ok_key, _passes_reader_check(body, ctype, page_url),
                  86_400 if _passes_reader_check(body, ctype, page_url) else 3600)
        # Meta tags live in <head> — first 60KB is way more than enough.
        html = body[:60_000]
    except Exception as exc:
        print(f"[og] {page_url[:60]} failed: {exc}")
        cache.set(key, "", 1800)
        cache.set(ok_key, False, 1800)
        return None

    for pat in (_OG_IMAGE_RE, _OG_IMAGE_REV_RE, _TWITTER_IMAGE_RE):
        m = pat.search(html)
        if m:
            img = unescape(m.group(1)).strip()
            if img.startswith("//"):
                img = "https:" + img
            cache.set(key, img, 86_400)   # 1 day
            return img

    cache.set(key, "", 3600)   # miss — re-try in an hour
    return None


def _is_readable(article_url: str) -> bool:
    """True if the page passed the trafilatura probe, or we haven't
    tried it yet (default to keeping it; it'll be tested next cycle)."""
    v = cache.get(f"reader_ok:{article_url}")
    return True if v is None else bool(v)


# URL patterns that we know are never extractable. Skip them up front
# rather than waste an HTTP fetch.
_DEAD_URL_PATTERNS = (
    "news.google.com/rss/articles/",   # Google News interstitial — body is JS
)


def _is_obviously_dead(url: str) -> bool:
    if not url:
        return True
    return any(p in url for p in _DEAD_URL_PATTERNS)


async def _enrich_missing_images(articles: list[Article]) -> None:
    """For articles without an image, scrape og:image from the article page
    in parallel (limited concurrency to be polite)."""
    missing = [a for a in articles if not a.image]
    if not missing:
        return

    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers=_OG_HEADERS, http2=False) as client:
        async def _one(article: Article) -> None:
            async with sem:
                got = await _fetch_og_image(client, article.url)
                if got:
                    article.image = got

        await asyncio.gather(*[_one(a) for a in missing])


async def _probe_readability(articles: list[Article]) -> None:
    """Make sure every article has a reader_ok verdict. Articles that
    were probed by _fetch_og_image already have one; this pass picks
    up articles that had an RSS image (so we never fetched their page)."""
    todo = [
        a for a in articles
        if cache.get(f"reader_ok:{a.url}") is None and not _is_obviously_dead(a.url)
    ]
    if not todo:
        return

    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers=_OG_HEADERS, http2=False) as client:
        async def _one(article: Article) -> None:
            async with sem:
                ok_key = f"reader_ok:{article.url}"
                try:
                    r = await client.get(article.url, timeout=8.0, follow_redirects=True)
                    if r.status_code >= 400:
                        cache.set(ok_key, False, 3600)
                        return
                    ok = _passes_reader_check(
                        r.text, r.headers.get("content-type", ""), article.url,
                    )
                    cache.set(ok_key, ok, 86_400 if ok else 3600)
                except Exception:
                    cache.set(ok_key, False, 1800)

        await asyncio.gather(*[_one(a) for a in todo])


def _parse_feed(raw_bytes: bytes, outlet: dict) -> list[Article]:
    parsed = feedparser.parse(raw_bytes)
    items: list[Article] = []
    for entry in parsed.entries[: config.PER_OUTLET_LIMIT]:
        title = entry.get("title", "(no title)").strip()
        # _extract_image searches every common RSS slot. Anything still
        # missing gets resolved later via og:image scrape + AI fallback.
        items.append(Article(
            title=title,
            url=entry.get("link", ""),
            outlet=outlet["name"],
            category=outlet["category"],
            lang=outlet.get("lang", "en"),
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

    # Dedupe by URL — outlets with multiple section feeds (e.g. 연합뉴스 +
    # 연합 경제) often repeat the same story.
    seen: set[str] = set()
    articles: list[Article] = []
    for batch in results:
        for a in batch:
            if a.url and a.url in seen:
                continue
            seen.add(a.url)
            articles.append(a)

    # Stage 2a: hard-drop obvious dead URLs (Google News interstitials etc.).
    articles = [a for a in articles if not _is_obviously_dead(a.url)]

    # Stage 2b: for articles whose feed didn't include an image, hit the
    # article page and pull og:image. Records `reader_ok:{url}` along the
    # way based on whether trafilatura can pull a real body.
    await _enrich_missing_images(articles)

    # Stage 2c: probe articles that already had an RSS image — we still
    # need to know if their page is readable. Cached per URL so only new
    # articles trigger HTTP on subsequent refreshes.
    await _probe_readability(articles)

    # Stage 3: drop articles whose page failed the trafilatura probe
    # (paywalls, JS-only pages, interstitials, dead links).
    before = len(articles)
    articles = [a for a in articles if _is_readable(a.url)]
    if (n := before - len(articles)):
        print(f"[rss] dropped {n} articles that failed extraction", flush=True)

    # Stage 4: drop articles without a real photo. The user explicitly
    # doesn't want feed tiles backed by AI-generated placeholders or
    # publisher-brand site logos — only articles with a genuine hero
    # image stay in the feed.
    before = len(articles)
    articles = [a for a in articles if a.image]
    if (n := before - len(articles)):
        print(f"[rss] dropped {n} articles with no image", flush=True)

    # Stage 5: drop articles whose "image" is actually the publisher's
    # generic share image. Heuristic: same (outlet, image_url) used by
    # 2+ articles is almost certainly a logo / default OG image — real
    # article photos are unique per story.
    from collections import Counter
    pair_counts = Counter((a.outlet, a.image) for a in articles)
    before = len(articles)
    articles = [a for a in articles if pair_counts[(a.outlet, a.image)] == 1]
    if (n := before - len(articles)):
        print(f"[rss] dropped {n} articles using a recycled outlet logo", flush=True)

    cache.set("rss:all", articles, config.FEED_CACHE_TTL)
    return articles

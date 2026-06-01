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

from backend import cache, config, db
from backend.agent import illustrator, reader as reader_agent


# Strip raw HTML tags (img, p, span, br, a, etc.) and orphan attribute
# fragments out of an RSS-supplied summary so they never reach the card.
# Some publishers (especially 매일경제 / 동아일보) leak <img border=0 …>
# tags and inline-style attributes into their RSS <description> field.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ORPHAN_OPEN_TAG_RE = re.compile(
    r"<\s*(img|figure|figcaption|span|div|p|br|table|tr|td|a|iframe|script|style)\b[^<]*",
    re.IGNORECASE,
)
_ATTR_FRAGMENT_RE = re.compile(
    r"\b(?:img|src|alt|width|height|border|style|class|figure|figcaption)\s*=\s*['\"][^'\"]*['\"]?",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _clean_summary(raw: str) -> str:
    """Two-pass scrub so partial / encoded HTML can't slip through.
      1. strip raw <…>
      2. unescape  (turns &lt;img&gt; into <img>)
      3. strip <…> AGAIN
      4. unescape once more
      5. kill orphan opening tags (`<img border…` w/ no `>`)
      6. kill leftover attribute fragments
      7. collapse whitespace
    """
    if not raw:
        return ""
    s = _HTML_TAG_RE.sub(" ", raw)
    s = unescape(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = unescape(s)
    s = _ORPHAN_OPEN_TAG_RE.sub(" ", s)
    s = _ATTR_FRAGMENT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


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


_IMG_TAG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
# Bigger pattern that pulls width/height attrs alongside src — used by
# _find_body_hero_image to pick the largest in-body photo when the og:image
# turns out to be a generic brand logo.
_IMG_TAG_RICH_RE = re.compile(
    r"""<img\b(?P<attrs>[^>]*?)>""", re.IGNORECASE
)
_SRC_ATTR_RE = re.compile(r"""(?:src|data-src|data-original)=["']([^"']+)["']""", re.IGNORECASE)
_WIDTH_ATTR_RE = re.compile(r"""width=["']?(\d+)""", re.IGNORECASE)
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
            m = _IMG_TAG_SRC_RE.search(val)
            if m:
                return unescape(m.group(1))

    return None


# ── Body sanitiser ─────────────────────────────────────────────────
# Card dek shouldn't start with leaked author-avatar markdown, a repeat
# of the title, or a reporter byline. Centralised here so backfill paths
# all get the same scrub. Tuned from observed failures: HuggingFace
# blogs leak `![](https://cdn-avatars…)`, Korean dailies prepend
# `[속보] 기자 …`, Reuters duplicates the headline as the lede.
_DEK_IMG_MD_RE = re.compile(r"!\[(?:[^\]\[]|\[[^\]]*\])*\]\([^)]*\)")
_DEK_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DEK_BRACKET_PREFIX_RE = re.compile(
    r"^\s*\[(?:속보|단독|영상|포토|사진|인터뷰|기획|특파원|업데이트|"
    r"BREAKING|EXCLUSIVE|UPDATE|VIDEO|PHOTOS?)\]\s*",
    re.IGNORECASE,
)
_DEK_BYLINE_RE = re.compile(
    r"^\s*(?:By\s+|글\s*[=:]|취재\s*[=:]|"
    r"(?:[가-힣]{2,4}\s+기자\s*[=·:]|[가-힣]{2,4}\s+특파원\s*[=·:]))",
    re.IGNORECASE,
)
_DEK_DATELINE_RE = re.compile(
    r"^\s*\(?(?:[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?|[가-힣]{2,6})\)?\s*[—–-]\s*",
)
_DEK_CREDIT_RE = re.compile(
    r"^\s*(?:사진|영상|이미지|일러스트|그래픽|자료(?:사진)?|"
    r"Photo|Image|Credit|©)\s*[=·:]",
    re.IGNORECASE,
)
_DEK_WS_RE = re.compile(r"\s+")


def _scrub_dek_line(s: str) -> str:
    """One-pass cleanup for a candidate dek line. Strips markdown
    images, HTML tags, common Korean/English byline + dateline prefixes,
    photo-credit prefixes, then collapses whitespace."""
    if not s:
        return ""
    s = _DEK_IMG_MD_RE.sub("", s)
    s = _DEK_HTML_TAG_RE.sub("", s)
    s = _DEK_BRACKET_PREFIX_RE.sub("", s)
    s = _DEK_BYLINE_RE.sub("", s)
    s = _DEK_DATELINE_RE.sub("", s)
    s = _DEK_CREDIT_RE.sub("", s)
    return _DEK_WS_RE.sub(" ", s).strip()


def _pick_dek(text: str, title: str = "") -> str:
    """Walk the article body and return the first real prose line.
    Skips: blank lines, lines shorter than 40 chars, lines that are
    just the title, lines that are mostly markdown/HTML/bracketed
    boilerplate, and image-markdown-only lines."""
    if not text:
        return ""
    title_norm = _DEK_WS_RE.sub(" ", (title or "").strip()).lower()
    for raw in text.split("\n"):
        line = _scrub_dek_line(raw)
        # 25 chars is enough to filter one-word lines / dividers without
        # accidentally dropping dense Korean sentences (which pack a lot
        # of meaning into fewer characters than English).
        if not line or len(line) < 25:
            continue
        # Skip lines that ARE the title (or contain it as the only content)
        norm = _DEK_WS_RE.sub(" ", line).lower()
        if title_norm and (norm == title_norm or norm.startswith(title_norm + " ")):
            continue
        # Skip lines that are mostly URLs
        if line.count("http") >= 2 and len(line) - line.count("http") * 8 < 40:
            continue
        return line
    return ""


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


# URL patterns that we know are never extractable. We used to drop
# news.google.com/rss/articles/ entries here, but that nuked the entire
# K-Ent category (which gets all its feeds via the Google News search
# proxy). Now we KEEP them: the user can still click through (Google
# redirects to the publisher) and the title/dek work fine. We only drop
# completely empty URLs.
_DEAD_URL_PATTERNS: tuple[str, ...] = ()


def _is_obviously_dead(url: str) -> bool:
    if not url:
        return True
    return any(p in url for p in _DEAD_URL_PATTERNS)


# URL fragments / filenames that strongly suggest a generic share image
# (publisher logo, default fallback, social-card placeholder).
_LOGO_HINT_PATTERNS = (
    "logo", "default", "share", "og-image",
    "og_default", "site-image", "favicon",
)


def _looks_like_logo(image_url: str) -> bool:
    if not image_url:
        return False
    u = image_url.lower()
    return any(p in u for p in _LOGO_HINT_PATTERNS)


def _find_body_hero_image(html: str, base_url: str = "") -> str | None:
    """Scan the article body for the largest <img> tag we can find — the
    one most likely to be the hero photo. Used to rescue articles whose
    og:image is just the publisher's brand share-card."""
    if not html:
        return None
    best_url: str | None = None
    best_width = 0
    # Cap how much HTML we scan — body photos almost always appear in the
    # first ~80KB; anything later is recommendations / footer chrome.
    for m in _IMG_TAG_RICH_RE.finditer(html[:80_000]):
        attrs = m.group("attrs")
        src_m = _SRC_ATTR_RE.search(attrs)
        if not src_m:
            continue
        src = unescape(src_m.group(1)).strip()
        if not src or src.startswith("data:"):
            continue
        if _looks_like_logo(src):
            continue
        # Skip very small icons / favicons by URL hint
        if any(s in src.lower() for s in ("/icon", "spinner", "spacer", "1x1")):
            continue
        # Width = best signal we have. Default to a hopeful 600 if not given.
        w_m = _WIDTH_ATTR_RE.search(attrs)
        width = int(w_m.group(1)) if w_m else 600
        # Require at least 300 px wide to count as a story photo.
        if width < 300:
            continue
        if width > best_width:
            best_width = width
            best_url = src
    if best_url and best_url.startswith("//"):
        best_url = "https:" + best_url
    return best_url


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
        cleaned_title = _clean_summary(title)[:300] or title
        raw_summary = _clean_summary(entry.get("summary", "") or "")
        # Strip image-markdown / brackets / bylines that publishers
        # frequently leak into RSS <description> (HuggingFace blog
        # author avatars, [속보] tags, Reuters dateline, etc.).
        cleaned_summary = _scrub_dek_line(raw_summary)[:400]
        items.append(Article(
            title=cleaned_title,
            url=entry.get("link", ""),
            outlet=outlet["name"],
            category=outlet["category"],
            lang=outlet.get("lang", "en"),
            summary=cleaned_summary,
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
    """Fast pass — RSS only. No HTTP probes, no body parsing. The expensive
    og:image scrape + trafilatura readability check is deferred to
    enrich_top() and runs only on the visible top-N items per refresh.
    On a 512MB Lightsail box, probing every one of 340 candidates was
    eating ~3 minutes of CPU per refresh. Probing 60 takes ~5 seconds."""
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

    # Cheap pattern-only filter: obvious dead URLs (Google News
    # interstitials etc.). No HTTP.
    articles = [a for a in articles if not _is_obviously_dead(a.url)]

    cache.set("rss:all", articles, config.FEED_CACHE_TTL)
    return articles


async def _rescue_logo_images(items: list[dict]) -> int:
    """For each item whose image is a known/recycled logo, refetch the
    article page and grab the largest in-body <img> as a replacement.
    Returns the count of rescues that actually swapped in a new URL."""
    if not items:
        return 0

    sem = asyncio.Semaphore(8)
    rescued = 0

    async with httpx.AsyncClient(headers=_OG_HEADERS, http2=False) as client:
        async def _one(item: dict) -> bool:
            nonlocal rescued
            async with sem:
                try:
                    r = await client.get(item["url"], timeout=8.0, follow_redirects=True)
                    if r.status_code >= 400:
                        return False
                    body_img = _find_body_hero_image(r.text, base_url=item["url"])
                    if body_img and body_img != item.get("image"):
                        item["image"] = body_img
                        rescued += 1
                        return True
                except Exception:
                    pass
                return False

        await asyncio.gather(*[_one(it) for it in items])
    return rescued


async def _backfill_bodies(items: list[dict]) -> int:
    """For ranked items whose RSS summary is empty/too short, fetch the
    article body via the reader agent, take the first ~280 chars as the
    card dek, and persist the full reader payload to SQLite so the
    modal renders instantly later. Returns how many we backfilled."""
    if not items:
        return 0
    need = [it for it in items if len((it.get("summary") or "").strip()) < 50 and it.get("url")]
    if not need:
        return 0

    sem = asyncio.Semaphore(6)
    filled = 0

    async def _one(item: dict) -> None:
        nonlocal filled
        async with sem:
            url = item["url"]
            title = item.get("title") or ""
            # Skip if we already have a stored reader payload — use it.
            stored = await db.get_reader(url)
            if stored:
                paras = stored.get("paragraphs") or []
                if paras:
                    cleaned = _pick_dek("\n".join(paras), title) or paras[0]
                    item["summary"] = cleaned[:400]
                    filled += 1
                return
            reading = await reader_agent.extract(url)
            if not reading or not (reading.text or reading.excerpt):
                return
            first_para = _pick_dek(reading.text or "", title)
            dek_src = first_para or _scrub_dek_line(reading.excerpt or "")
            dek = dek_src[:400]
            if not dek:
                return
            item["summary"] = dek
            # Persist what we scraped so the reader modal can serve it
            # without re-fetching. Shape mirrors what /api/article returns.
            try:
                payload = {
                    "url": url,
                    "title": reading.title or item.get("title") or "",
                    "image": reading.image or item.get("image"),
                    "byline": reading.byline,
                    "excerpt": reading.excerpt or "",
                    "lang": item.get("lang", "en"),
                    "word_count": len(reading.text.split()) if reading.text else 0,
                    "paragraphs": [
                        p.strip() for p in (reading.text or "").split("\n")
                        if len(p.strip()) >= 30
                    ][:14],
                }
                await db.save_reader(url, payload)
            except Exception as exc:
                print(f"[rss] save_reader failed for {url[:60]}: {exc}")
            filled += 1

    await asyncio.gather(*[_one(it) for it in need])
    return filled


async def enrich_top(articles: list[dict], top_n: int = 60) -> list[dict]:
    """Probe + filter only the first `top_n` curator-ranked items. Returns
    the survivors as a fresh list (other items can still be paginated to
    but won't have been quality-checked).

    Operates on the dict shape that curator.rank returns, not on Article
    dataclasses, because by the time we get here the curator has already
    converted them.
    """
    head = list(articles[:top_n])
    if not head:
        return head

    # Wrap dicts in lightweight objects so the existing probe helpers
    # (which read .url and .image) just work.
    class _Box:
        __slots__ = ("url", "image", "outlet", "_src")
        def __init__(self, d):
            self.url = d.get("url", "")
            self.image = d.get("image")
            self.outlet = d.get("outlet", "")
            self._src = d

    boxes = [_Box(d) for d in head]

    # Stage A: pull og:image for the ones missing a feed image. Also
    # records reader_ok:{url} as a side effect.
    await _enrich_missing_images(boxes)

    # Stage B: probe readability of the ones that already had an image.
    # (Cached per URL → only fresh URLs touch the network.)
    await _probe_readability(boxes)

    # Stage C: relaxed pass. We used to drop unreadable + image-less
    # items here, which nuked the entire K-Ent category (all Google
    # News-proxied). Now we keep everything — text-only items naturally
    # flow into the no-image tier on the frontend, and the sorter +
    # curator already handle quality control upstream.
    keep: list[dict] = []
    dropped_unread = dropped_noimg = 0
    for box in boxes:
        if box.image:
            box._src["image"] = box.image  # propagate newly-scraped image
        keep.append(box._src)

    # Stage D: identify recycled outlet logos (same image used by 2+
    # items from the same outlet) AND known-logo URL patterns.
    from collections import Counter
    pair_counts = Counter((d.get("outlet"), d.get("image")) for d in keep)
    needs_rescue: list[dict] = []
    for d in keep:
        img = d.get("image", "")
        if pair_counts[(d.get("outlet"), img)] >= 2 or _looks_like_logo(img):
            needs_rescue.append(d)

    # Stage E: instead of dropping logo-image articles, try to rescue
    # them by going back into the article page and pulling the biggest
    # in-body <img>. Anything we can't rescue does get dropped.
    rescued = await _rescue_logo_images(needs_rescue) if needs_rescue else 0
    after_rescue: list[dict] = []
    dropped_logo = 0
    pair_counts = Counter((d.get("outlet"), d.get("image")) for d in keep)
    for d in keep:
        img = d.get("image", "")
        # If the image is STILL the same recycled logo, drop the article.
        if (pair_counts[(d.get("outlet"), img)] >= 2 or _looks_like_logo(img)):
            dropped_logo += 1
            continue
        after_rescue.append(d)
    keep = after_rescue

    if dropped_unread or dropped_noimg or dropped_logo or rescued:
        print(
            f"[rss] enrich_top probed {len(boxes)}: "
            f"dropped {dropped_unread} unread + {dropped_noimg} no-image, "
            f"rescued {rescued} logo→hero, dropped {dropped_logo} unrescued → "
            f"{len(keep)} kept",
            flush=True,
        )

    # Stage F: any survivor still lacking a real body summary gets one
    # backfilled from trafilatura, and the full reader payload is saved
    # to SQLite so the user gets instant body text in the card AND in
    # the reader modal.
    backfilled = await _backfill_bodies(keep)
    if backfilled:
        print(f"[rss] enrich_top backfilled {backfilled} bodies", flush=True)
    return keep

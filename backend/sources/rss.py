"""RSS aggregator. Fetches each outlet in parallel and returns normalized items."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlparse

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
_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg)(?:$|[?#])", re.IGNORECASE)
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


def _clean_image_url(raw: str | None, base: str | None = None) -> str | None:
    """Validate + absolutise an image URL pulled out of an RSS entry.

    The feeds lie constantly: unquoted/short-circuited <img> markup makes
    the src regex capture things like "border=0", Hacker News hands back
    bare filenames ("NewAvatar_64.png"), and Korean dailies emit
    entity-encoded root-relative paths. Anything that isn't a real
    absolute http(s) URL after cleaning is dropped — a missing image
    falls back to the category gradient, which looks fine, whereas a
    junk one renders as a broken-image 404 against our own domain.
    """
    if not raw or not isinstance(raw, str):
        return None
    url = unescape(raw.strip()).replace("&#x2F;", "/").replace("&#47;", "/")
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        if not base:
            return None
        # Before joining, sanity-check that this even looks like a path.
        # A capture like "border=0" is a stray attribute the src regex
        # picked up, not a URL, and joining it would happily produce
        # https://outlet.example/border=0.
        looks_like_path = "/" in url or _IMG_EXT_RE.search(url)
        if not looks_like_path or ("=" in url.split("?", 1)[0] and "/" not in url):
            return None
        url = urljoin(base, url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    # A real host has a dot and no whitespace/= sign — that alone throws
    # out every observed junk value.
    host = parsed.netloc
    if not host or "." not in host or any(ch in host for ch in " ="):
        return None
    return url


def _extract_image(entry) -> str | None:
    """First pass — look at every place an RSS feed can stash an image."""
    # Relative paths in a feed are relative to the article, so keep the
    # entry link around as the base for _clean_image_url.
    base = entry.get("link") or ""

    # 1. media:content / media:thumbnail
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media and isinstance(media, list) and media:
            got = _clean_image_url(media[0].get("url"), base)
            if got:
                return got

    # 2. enclosure / link with image type
    for link in entry.get("links", []):
        if link.get("type", "").startswith("image/") and link.get("href"):
            got = _clean_image_url(link["href"], base)
            if got:
                return got

    # 3. itunes:image (some podcast-y feeds)
    itunes_img = entry.get("itunes_image") or entry.get("image")
    if isinstance(itunes_img, dict):
        got = _clean_image_url(itunes_img.get("href") or itunes_img.get("url"), base)
        if got:
            return got

    # 4. First <img src="…"> inside any HTML content field
    for field in ("content", "summary_detail", "summary", "description"):
        val = entry.get(field)
        if isinstance(val, list) and val:
            val = val[0]
        if isinstance(val, dict):
            val = val.get("value", "")
        if isinstance(val, str) and val:
            for m in _IMG_TAG_SRC_RE.finditer(val):
                got = _clean_image_url(m.group(1), base)
                if got:
                    return got

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


# ── Google News URL unwrapper ──────────────────────────────────────
# K-Ent (and lots of Korean outlets) feed through Google News' search
# RSS, which returns encoded `/articles/CBMi…` URLs that hide the real
# publisher URL behind a JS interstitial. Without unwrapping these we
# can never reach the publisher's og:image — every kent card ends up
# image-less.
_GNEWS_ARTICLE_RE = re.compile(r"news\.google\.com/(?:rss/)?articles/([^/?&]+)")
_NAU_RE = re.compile(r'data-n-au=["\']([^"\']+)["\']', re.IGNORECASE)
_META_REFRESH_URL_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']refresh["'][^>]+content=["'][^;]*;\s*url=([^"'>\s]+)""",
    re.IGNORECASE,
)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:location\.replace|location\.href|window\.location)\s*=\s*["\']([^"\']+)["\']',
)
# The modern Google News article shell carries the signature + timestamp
# needed by the batchexecute RPC (see _gnews_rpc_resolve).
_GN_SIG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS_RE  = re.compile(r'data-n-a-ts="([^"]+)"')


def _decode_gnews_article_url(url: str) -> str | None:
    """Pull the publisher URL out of a Google News article URL by
    base64-decoding the article ID and grepping the bytes for an
    http(s) URL. Works on a meaningful subset of the encoded payloads
    (the protobuf isn't standardised so this isn't 100%)."""
    import base64
    m = _GNEWS_ARTICLE_RE.search(url)
    if not m:
        return None
    encoded = m.group(1)
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except Exception:
        return None
    text = decoded.decode("latin-1", errors="ignore")
    candidates = re.findall(r"https?://[^\s\x00-\x1f\"'<>]+", text)
    for u in candidates:
        if "google." in u or "gstatic." in u:
            continue
        return u.split("\x00", 1)[0].strip()
    return None


_GNEWS_RPC_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def _gnews_article_id(url: str) -> str | None:
    """The opaque `CBMi…` article id out of any Google News URL shape."""
    m = _GNEWS_ARTICLE_RE.search(url)
    return m.group(1) if m else None


async def _gnews_rpc_resolve(
    client: httpx.AsyncClient, gnews_url: str,
) -> str | None:
    """Resolve a Google News link via Google's own batchexecute RPC.

    Google retired the old interstitial in 2025: the article id is now an
    encrypted blob (so base64-decoding it yields no URL), and the page no
    longer carries data-n-au / canonical / meta-refresh pointing at the
    publisher. The only route left is the `Fbv4je` RPC that the Google
    News SPA itself calls.

    Two steps:
      1. GET the article shell **with `?oc=5`** — without that parameter
         Google answers 400 Bad Request, which is exactly why every
         Google-News-sourced card started failing to open. The shell
         carries `data-n-a-sg` (signature) + `data-n-a-ts` (timestamp).
      2. POST id/ts/signature to batchexecute; the response embeds the
         real publisher URL.
    """
    art_id = _gnews_article_id(gnews_url)
    if not art_id:
        return None

    try:
        shell = await client.get(
            f"https://news.google.com/rss/articles/{art_id}?oc=5",
            timeout=10.0, follow_redirects=True, headers=_OG_HEADERS,
        )
        if shell.status_code != 200:
            return None
        body = shell.text
        sig = _GN_SIG_RE.search(body)
        ts = _GN_TS_RE.search(body)
        if not sig or not ts:
            return None
    except Exception:
        return None

    inner = json.dumps(
        [
            "garturlreq",
            [
                ["X", "X", ["X", "X"], None, None, 1, 1, "US:en",
                 None, 1, None, None, None, None, None, 0, 1],
                "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
            ],
            art_id,
            int(ts.group(1)),
            sig.group(1),
        ],
        separators=(",", ":"),
    )
    payload = json.dumps([[["Fbv4je", inner, None, "generic"]]], separators=(",", ":"))

    try:
        resp = await client.post(
            _GNEWS_RPC_ENDPOINT,
            data={"f.req": payload},
            timeout=10.0,
            headers={
                **_OG_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        if resp.status_code != 200:
            return None
        # Response is JSONP-guarded ()]}'\n) newline-delimited JSON arrays.
        for line in resp.text.splitlines():
            if "Fbv4je" not in line:
                continue
            try:
                frames = json.loads(line)
            except json.JSONDecodeError:
                continue
            for frame in frames:
                if (isinstance(frame, list) and len(frame) > 2
                        and frame[1] == "Fbv4je" and frame[2]):
                    parsed = json.loads(frame[2])
                    # ["garturlres", "<real url>", 1]
                    if len(parsed) > 1 and str(parsed[1]).startswith("http"):
                        return parsed[1]
    except Exception:
        return None
    return None


async def _resolve_gnews_url(
    client: httpx.AsyncClient, gnews_url: str,
) -> str:
    """Resolve a Google News article URL to its real publisher URL.
    Returns the resolved URL, or the original URL if resolution fails.
    Cached per URL for 1 day on success, 1 hour on failure."""
    if not gnews_url or "news.google.com" not in gnews_url:
        return gnews_url
    ckey = f"gnews_resolve:{gnews_url}"
    cached = cache.get(ckey)
    if cached is not None:
        return cached or gnews_url

    # Strategy 1: the batchexecute RPC. This is the only one that still
    # works on post-2025 Google News links, so it goes first.
    rpc = await _gnews_rpc_resolve(client, gnews_url)
    if rpc and "news.google.com" not in rpc:
        cache.set(ckey, rpc, 86_400)
        return rpc

    # Strategy 2: plain redirect / interstitial scrape. Still catches the
    # older `/articles/` links that predate the RPC-only scheme.
    try:
        r = await client.get(
            gnews_url, timeout=8.0, follow_redirects=True, headers=_OG_HEADERS,
        )
        final = str(r.url)
        if "news.google.com" not in final and final.startswith("http"):
            cache.set(ckey, final, 86_400)
            return final
        body = r.text[:120_000]
        for pat in (_NAU_RE, _META_REFRESH_URL_RE, _CANONICAL_RE, _JS_REDIRECT_RE):
            m = pat.search(body)
            if m:
                u = m.group(1).strip()
                if u and "news.google.com" not in u and u.startswith("http"):
                    cache.set(ckey, u, 86_400)
                    return u
    except Exception:
        pass

    # Strategy 3: base64-decode the article ID. Only works on the legacy
    # plaintext payloads; kept as a last resort for old cached links.
    decoded = _decode_gnews_article_url(gnews_url)
    if decoded:
        cache.set(ckey, decoded, 86_400)
        return decoded

    cache.set(ckey, "", 3600)   # known failure — retry in an hour
    return gnews_url


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

    # If this is a Google News redirect URL (every kent feed, plus most
    # of the naver category), resolve to the real publisher URL first.
    # We still cache under the gnews URL so the lookup path is stable.
    fetch_url = page_url
    if "news.google.com" in page_url:
        resolved = await _resolve_gnews_url(client, page_url)
        if resolved and resolved != page_url:
            fetch_url = resolved

    try:
        r = await client.get(fetch_url, timeout=8.0, follow_redirects=True, headers=_OG_HEADERS)
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
# Pages that are never articles, matched on URL so we never spend a
# fetch on them. Al Jazeera's /video/newsfeed/ entries come through the
# same RSS feed as its reporting but are video players — the "body" is
# the headline twice and a one-line caption.
_DEAD_URL_PATTERNS: tuple[str, ...] = (
    "aljazeera.com/video/",
    "/gallery/",
    "/photos/",
)


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
    """Decide, for every article, whether we can actually show its body.

    Runs the *same* extractor the reader modal uses rather than a
    parallel heuristic, so a story can never pass ingest and then fail
    to open. That also means Google News links get resolved to the
    publisher first — the old bespoke check fetched the Google shell and
    marked every Google-sourced article unreadable.

    Extraction results are cached per URL by the reader agent, so the
    body is already warm by the time the user clicks.
    """
    todo = [
        a for a in articles
        if cache.get(f"reader_ok:{a.url}") is None and not _is_obviously_dead(a.url)
    ]
    if not todo:
        return

    sem = asyncio.Semaphore(8)

    async def _one(article: Article) -> None:
        async with sem:
            ok_key = f"reader_ok:{article.url}"
            try:
                reading, _err = await reader_agent.extract_detailed(article.url)
            except Exception:
                cache.set(ok_key, False, 1800)
                return
            ok = bool(reading)
            # Success is cached for a day; failure for an hour, so a
            # transient block doesn't exile an outlet until tomorrow.
            cache.set(ok_key, ok, 86_400 if ok else 3600)
            # The extracted body itself stays in the reader agent's own
            # 24h cache, so the first click serves from memory without
            # re-fetching. Shaping it into the stored reader payload is
            # /api/article's job — doing it here would persist a
            # half-formed row that the reader would then serve verbatim.

    await asyncio.gather(*[_one(a) for a in todo])


def _parse_feed(raw_bytes: bytes, outlet: dict) -> list[Article]:
    parsed = feedparser.parse(raw_bytes)
    items: list[Article] = []
    for entry in parsed.entries[: config.PER_OUTLET_LIMIT]:
        title = entry.get("title", "(no title)").strip()
        # _extract_image searches every common RSS slot. Anything still
        # missing gets resolved later via og:image scrape + AI fallback.
        cleaned_title = _clean_summary(title)[:300] or title
        cleaned_title = _polish_title(cleaned_title)
        # Drop photo-series / video-only entries entirely. They show up
        # as cards with one image and no real news ([포토] / [영상] /
        # [사진] / [Gallery] / "Photos:" / etc.) and the user explicitly
        # asked to cut them.
        if _is_series_filler(cleaned_title):
            continue
        raw_summary = _clean_summary(entry.get("summary", "") or "")
        # Strip image-markdown / brackets / bylines that publishers
        # frequently leak into RSS <description> (HuggingFace blog
        # author avatars, [속보] tags, Reuters dateline, etc.).
        # Cap is language-aware: Korean is denser per character so a
        # shorter cap produces a comparable visual block to the longer
        # English cap. User specifically asked to even out the two.
        outlet_lang = (outlet.get("lang") or "en").lower()
        summary_cap = 240 if outlet_lang.startswith("ko") else 400
        cleaned_summary = _scrub_dek_line(raw_summary)[:summary_cap]
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


# ── Title polish + photo-series filter ─────────────────────────────
# Outlet self-attribution that Google News + some KR outlets append:
#   "Real Title - 매일경제 영문뉴스 펄스(Pulse)"
#   "Real Title — 매일경제"
#   "Real Title | Chosun Biz"
# We don't want that suffix on the card. The dedup module already
# strips a similar pattern for signature comparison; this version
# runs at ingest time so the saved title is clean for everyone.
_TITLE_OUTLET_TAIL_RE = re.compile(
    r"\s*[-—–·|]\s+(?:[A-Z가-힣][\w&'.()가-힣]*\s*){1,6}$"
)
# Standalone-leading "[속보]" / "[BREAKING]" / "[단독]" are signal
# words — leave them. But "[포토]" / "[영상]" / "[사진]" / "[Photos]"
# / "[Gallery]" / "Photos:" / "PHOTOS:" mark image-only cards we want
# to drop.
_SERIES_FILLER_RE = re.compile(
    r"^\s*(?:"
    r"\[(?:포토|영상|사진|화보|갤러리|Photos?|Gallery|Slideshow|Watch|Video)[^\]]*\]"
    r"|(?:Photos?|Gallery|Watch|Video|Slideshow)\s*:"
    r")\s*",
    re.IGNORECASE,
)


def _polish_title(title: str) -> str:
    """Light title cleanup applied at ingest. Drops trailing outlet
    self-attribution + collapses whitespace."""
    if not title:
        return ""
    # Strip outlet tail repeatedly so "headline - Site - Network"
    # collapses cleanly.
    prev = None
    while prev != title:
        prev = title
        title = _TITLE_OUTLET_TAIL_RE.sub("", title).strip()
    # Collapse runs of whitespace.
    title = _WS_RE.sub(" ", title).strip()
    return title


def _is_series_filler(title: str) -> bool:
    """True for photo / video / gallery teasers we drop on ingest."""
    if not title:
        return True
    return bool(_SERIES_FILLER_RE.match(title))


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
                    fetch_url = item["url"]
                    if "news.google.com" in fetch_url:
                        resolved = await _resolve_gnews_url(client, fetch_url)
                        if resolved and resolved != fetch_url:
                            fetch_url = resolved
                    r = await client.get(fetch_url, timeout=8.0, follow_redirects=True)
                    if r.status_code >= 400:
                        return False
                    body_img = _find_body_hero_image(r.text, base_url=fetch_url)
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
            lang = (item.get("lang") or "en").lower()
            # Match the per-language cap used by main._dek_cap so the
            # backfill never produces a longer dek than the resummary
            # worker would. Keeps EN ↔ KO cards visually balanced.
            dek_cap = 240 if lang.startswith("ko") else 440
            # Korean publishers often pack the whole lede into one
            # dense paragraph — joining two would blow past the cap.
            max_paras_for_lang = 1 if lang.startswith("ko") else 2
            # Skip if we already have a stored reader payload — use it.
            stored = await db.get_reader(url)
            if stored:
                paras = stored.get("paragraphs") or []
                if paras:
                    cleaned = " ".join(
                        p.strip() for p in paras[:max_paras_for_lang] if p.strip()
                    )[:dek_cap]
                    if cleaned:
                        item["summary"] = cleaned
                        filled += 1
                return
            reading = await reader_agent.extract(url)
            if not reading or not (reading.text or reading.excerpt):
                return
            # Walk the body for the first prose paragraph(s), joined.
            text = reading.text or ""
            picks: list[str] = []
            seen = 0
            for raw in text.split("\n"):
                line = _scrub_dek_line(raw)
                if line and len(line) >= 25:
                    picks.append(line)
                    seen += 1
                    if seen >= max_paras_for_lang:
                        break
            dek_src = " ".join(picks) if picks else _scrub_dek_line(reading.excerpt or "")
            dek = dek_src[:dek_cap]
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

    # Stage C: drop anything whose body we can't actually show.
    #
    # This gate was written, then disabled, because the old readability
    # check fetched Google News shells and so condemned the entire K-Ent
    # category. _probe_readability now runs the real reader (which
    # resolves Google News first), so the verdict is trustworthy and the
    # gate is back on.
    #
    # The rule: if there is no article body, the story does not go in
    # the list. A headline plus a two-line RSS blurb is not an article —
    # showing one is worse than showing nothing, because the user
    # clicked expecting to read something.
    keep: list[dict] = []
    dropped_unread = dropped_noimg = 0
    dropped_by_outlet: dict[str, int] = {}
    for box in boxes:
        if not _is_readable(box.url):
            dropped_unread += 1
            dropped_by_outlet[box.outlet] = dropped_by_outlet.get(box.outlet, 0) + 1
            continue
        if box.image:
            box._src["image"] = box.image  # propagate newly-scraped image
        keep.append(box._src)

    if dropped_unread:
        worst = sorted(dropped_by_outlet.items(), key=lambda kv: -kv[1])[:6]
        print(
            f"[rss] dropped {dropped_unread} unreadable articles "
            f"(no body): {worst}",
            flush=True,
        )

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

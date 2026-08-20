"""Reader agent — given an article URL, returns a clean extraction of
the main body, title, image, byline.

Powered by trafilatura, which is the go-to Python lib for this. Far
lighter than Hermes / scrapyd / chromium-based scrapers and works on
just about every news site we hit.

Every extraction records *why* it failed (`ExtractError.reason`) so the
outlet-health view can tell "the publisher paywalled us" apart from
"our parser gave up", instead of collapsing both into one dead error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from backend import cache


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# A bare User-Agent alone trips the bot heuristics at a lot of outlets
# (they check for the header set a real Chrome sends, not just the UA
# string). Sending an honest, complete browser header set is what got
# Ars/Verge/Nikkei-class sites answering 200 instead of 403. Sites that
# genuinely paywall still paywall — that path falls back to the stored
# summary rather than trying to get around anything.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Below this many words we treat the body as "not really an article" and
# escalate to the next fallback.
MIN_BODY_WORDS = 60
# Korean packs far more meaning per whitespace token than English, so a
# word floor would reject perfectly complete Korean articles. Count
# characters there instead.
MIN_BODY_CHARS_KO = 300


def _is_korean(text: str) -> bool:
    """True if the text is predominantly Hangul."""
    if not text:
        return False
    hangul = sum(1 for ch in text[:600] if "\uac00" <= ch <= "\ud7a3")
    return hangul >= 40


def body_is_substantial(text: str) -> bool:
    """Is this enough text to be an actual article?

    The gate that decides whether a story is worth putting in the feed
    at all. A headline plus a two-line RSS blurb is not an article — the
    user was explicit that showing one is worse than showing nothing.
    """
    if not text:
        return False
    if _is_korean(text):
        return len(text.replace(" ", "")) >= MIN_BODY_CHARS_KO
    return len(text.split()) >= MIN_BODY_WORDS


@dataclass
class Reading:
    url: str
    title: str
    image: str | None
    byline: str | None
    text: str
    excerpt: str
    # The publisher URL we actually read, after unwrapping Google News.
    # The reader modal links to this so "open original" lands on the
    # outlet rather than back on a Google interstitial.
    final_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtractError:
    """Why an extraction produced nothing. `reason` is a stable slug the
    outlet-health view groups on."""
    reason: str          # blocked | paywall | notfound | timeout | empty | error
    status: int | None = None
    detail: str = ""
    final_url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _absolutize(image: str | None, base_url: str) -> str | None:
    """Make an extracted image URL absolute against the page it came from.

    trafilatura hands back whatever the markup held, which is often a
    root-relative path ("/resources/images/x.jpg"). Stored raw, the
    browser resolves it against *our* domain and 404s — HBR photos were
    being requested from dailybrief.fun. Anything that isn't http(s)
    after joining (data:, blank) is dropped rather than stored broken.
    """
    if not image:
        return None
    image = image.strip()
    if not image or image.startswith("data:"):
        return None
    if image.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        return f"{scheme}:{image}"
    if not image.startswith(("http://", "https://")):
        image = urljoin(base_url, image)
    return image if image.startswith(("http://", "https://")) else None


def _reason_for_status(status: int) -> str:
    if status in (401, 402):
        return "paywall"
    if status in (403, 429):
        return "blocked"
    if status in (404, 410):
        return "notfound"
    return "error"


async def extract_detailed(url: str) -> tuple[Reading | None, ExtractError | None]:
    """Same as `extract`, but also reports the failure reason.

    Returns `(reading, None)` on success, `(None, error)` on failure.
    Results are cached for 1 day per URL; failures for 30 min so a
    transient block clears on its own.
    """
    if not url:
        return None, ExtractError(reason="error", detail="empty url")

    cache_key = f"reader:{url}"
    cached = cache.get(cache_key)
    if cached is not None:
        if isinstance(cached, ExtractError):
            return None, cached
        return cached, None

    fetch_url = url
    try:
        async with httpx.AsyncClient(headers=BROWSER_HEADERS) as client:
            # Google News URLs hide the publisher behind an RPC-gated
            # shell. Resolve to the real publisher URL so trafilatura has
            # a real page to extract from (otherwise every Google-sourced
            # article returns an empty body). Import is lazy to avoid the
            # rss <- reader cycle.
            if "news.google.com" in url:
                from backend.sources.rss import _resolve_gnews_url
                resolved = await _resolve_gnews_url(client, url)
                if resolved and resolved != url:
                    fetch_url = resolved

            r = await client.get(fetch_url, follow_redirects=True, timeout=15.0)
            if r.status_code >= 400:
                err = ExtractError(
                    reason=_reason_for_status(r.status_code),
                    status=r.status_code,
                    detail=f"HTTP {r.status_code}",
                    final_url=fetch_url,
                )
                cache.set(cache_key, err, 1800)
                print(f"[reader] {fetch_url[:70]} → HTTP {r.status_code}")
                return None, err
            html = r.text
            fetch_url = str(r.url) or fetch_url
    except httpx.TimeoutException:
        err = ExtractError(reason="timeout", detail="fetch timed out", final_url=fetch_url)
        cache.set(cache_key, err, 1800)
        print(f"[reader] fetch {url[:70]} timed out")
        return None, err
    except Exception as exc:
        err = ExtractError(reason="error", detail=f"{type(exc).__name__}", final_url=fetch_url)
        cache.set(cache_key, err, 1800)
        print(f"[reader] fetch {url[:70]} failed: {exc}")
        return None, err

    data_json = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_images=True,
        include_links=False,
        favor_recall=True,
        url=fetch_url,
    )

    title = ""
    image = None
    byline = None
    text = ""
    excerpt = ""

    if data_json:
        try:
            data = json.loads(data_json)
            title   = (data.get("title") or "").strip()
            image   = _absolutize(data.get("image"), fetch_url)
            byline  = data.get("author")
            text    = (data.get("text") or data.get("raw_text") or "").strip()
            excerpt = (data.get("description") or "").strip()[:300]
        except json.JSONDecodeError:
            pass

    # Body looked thin → fallback agent: re-run trafilatura with a
    # different config, then if it still comes up short pull every
    # <p> tag straight out of the HTML body. Many Korean outlets
    # (mk.co.kr, donga.com) gate behind weird wrappers that trafilatura
    # gives up on at its first pass.
    if len(text.split()) < MIN_BODY_WORDS:
        alt = trafilatura.extract(
            html,
            output_format="txt",
            include_links=False,
            favor_recall=True,
            no_fallback=False,
            include_comments=False,
            include_tables=False,
            url=fetch_url,
        )
        if alt and len(alt.split()) > len(text.split()):
            text = alt.strip()

    if len(text.split()) < MIN_BODY_WORDS:
        fb = _extract_paragraphs_fallback(html)
        if fb and len(fb.split()) > len(text.split()):
            text = fb

    if not body_is_substantial(text):
        # A title with no real body is not an article. Previously this
        # passed through whenever a title existed, which is how
        # headline-plus-blurb stubs reached the feed.
        err = ExtractError(
            reason="empty",
            status=200,
            detail=(
                "no body found in page" if not text
                else f"body too thin ({len(text.split())} words)"
            ),
            final_url=fetch_url,
        )
        cache.set(cache_key, err, 1800)
        return None, err

    reading = Reading(
        url=url,
        title=title,
        image=image,
        byline=byline,
        text=text,
        excerpt=excerpt,
        final_url=fetch_url if fetch_url != url else None,
    )
    cache.set(cache_key, reading, 86400)
    return reading, None


async def extract(url: str) -> Reading | None:
    """Fetch the page and pull a clean Reading. None if extraction fails."""
    reading, _ = await extract_detailed(url)
    return reading


# ── Fallback paragraph extractor ─────────────────────────────────
# Last-ditch: pull text from every <p> / <div class="article-body">
# tag in the HTML, scrubbing tags + entities. Slower than trafilatura
# but catches sites trafilatura misclassifies as boilerplate.
import re as _re

_P_TAG_RE = _re.compile(r"<p[^>]*>(.*?)</p>", _re.DOTALL | _re.IGNORECASE)
_BODY_DIV_RE = _re.compile(
    r'<(?:article|div)[^>]*(?:class|id)=["\'][^"\']*'
    r'(?:article(?:[-_]body)?|story[-_]body|news[-_]body|content[-_]body|read[-_]body)'
    r'[^"\']*["\'][^>]*>(.*?)</(?:article|div)>',
    _re.DOTALL | _re.IGNORECASE,
)
_SCRIPT_STYLE_RE = _re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", _re.DOTALL | _re.IGNORECASE
)
_TAG_RE = _re.compile(r"<[^>]+>")
from html import unescape as _unescape


def _extract_paragraphs_fallback(html_text: str) -> str:
    if not html_text:
        return ""
    # Strip script/style first — otherwise inline JS containing "<p>"
    # strings leaks into the body as garbage paragraphs.
    html_text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    chunks: list[str] = []
    m = _BODY_DIV_RE.search(html_text)
    pool = m.group(1) if m else html_text
    for pm in _P_TAG_RE.finditer(pool):
        raw = pm.group(1)
        raw = _TAG_RE.sub("", raw)
        raw = _unescape(raw)
        raw = raw.strip()
        if len(raw) >= 30:
            chunks.append(raw)
    return "\n".join(chunks)

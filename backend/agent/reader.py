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

    title = ""
    image = None
    byline = None
    text = ""
    excerpt = ""

    if data_json:
        try:
            data = json.loads(data_json)
            title   = (data.get("title") or "").strip()
            image   = data.get("image")
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
    if len(text.split()) < 60:
        alt = trafilatura.extract(
            html,
            output_format="txt",
            include_links=False,
            favor_recall=True,
            no_fallback=False,
            include_comments=False,
            include_tables=False,
            url=url,
        )
        if alt and len(alt.split()) > len(text.split()):
            text = alt.strip()

    if len(text.split()) < 60:
        text = _extract_paragraphs_fallback(html) or text

    if not text and not title:
        return None

    reading = Reading(
        url=url,
        title=title,
        image=image,
        byline=byline,
        text=text,
        excerpt=excerpt,
    )
    cache.set(cache_key, reading, 86400)
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
_TAG_RE = _re.compile(r"<[^>]+>")
from html import unescape as _unescape


def _extract_paragraphs_fallback(html_text: str) -> str:
    if not html_text:
        return ""
    # First try the article-body container.
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

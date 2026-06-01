"""FastAPI app: serves API + static frontend."""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import cache, config, mixer, tickers
from backend.agent import curator, illustrator, reader, summary
from backend.sources import politicians, prices, rss, whales, youtube

load_dotenv()

app = FastAPI(title="dailybrief")


# ── Background refresh agent ─────────────────────────────────────────
# Runs hourly. Each tick clears the feed cache so the next /api/brief
# hits fresh RSS. We deliberately do NOT pre-probe anything here —
# probing is now done lazily inside /api/brief on just the top-N items.
AGENT_INTERVAL_SECONDS = 3600


async def _refresh_agent():
    await asyncio.sleep(20)  # let the app start serving first
    while True:
        try:
            # Drop the feed-level cache. Per-URL probe caches stay
            # (24h success, 1h fail) so we don't re-fetch known-good URLs.
            for k in list(cache._store.keys()):
                if k.startswith("rss:") or k.startswith("article_reader:"):
                    cache._store.pop(k, None)
            print(f"[agent] refresh cycle: cleared feed cache", flush=True)
        except Exception as exc:
            print(f"[agent] tick failed: {exc}", flush=True)
        await asyncio.sleep(AGENT_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_agent():
    asyncio.create_task(_refresh_agent())

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/brief")
async def brief():
    """The main payload: ticker tape + mixed feed + sidebars + summary."""
    # Full-response cache — saves the user 5-15 s of curator + sparkline
    # work on every load. Short TTL so freshness is preserved.
    cached = cache.get("brief:response")
    if cached is not None:
        return cached

    articles, whale_moves_raw, insider_trades_raw, yt_raw, tape = await asyncio.gather(
        rss.fetch_all(),
        whales.fetch(),
        politicians.fetch(),
        youtube.fetch(),
        prices.fetch_tape(),
    )

    # Over-fetch so we still hit TOP_K visible items after enrich_top
    # throws out paywalls / logos.
    raw_top = await curator.rank(articles, top_k=int(config.TOP_K * 1.7))
    # Probe + filter only this ranked head — way cheaper than touching
    # every RSS candidate.
    top = await rss.enrich_top(raw_top, top_n=int(config.TOP_K * 1.5))
    top = top[: config.TOP_K]
    await tickers.enrich_with_sparks(top)

    by_outlet: dict[str, list[dict]] = {}
    for a in articles:
        by_outlet.setdefault(a.outlet, []).append(a.to_dict())

    whale_moves = [w.to_dict() for w in whale_moves_raw]
    insider_trades = [t.to_dict() for t in insider_trades_raw]
    yt = [y.to_dict() for y in yt_raw]

    mixed = mixer.merge(
        mixer.from_news(top),
        mixer.from_whales(whale_moves),
        mixer.from_trades(insider_trades),
        mixer.from_videos(yt),
    )

    headline = await summary.generate(mixed)

    payload = {
        "profile": {
            "bio": config.PROFILE.bio,
            "keywords": config.PROFILE.keywords,
            "primary_lang": getattr(config.PROFILE, "primary_lang", "en"),
        },
        "tape": [q.to_dict() for q in tape],
        "headline": headline,
        "mixed": mixed,
        "top": top,
        "by_outlet": by_outlet,
        "whales": whale_moves,
        "trades": insider_trades,
        "youtube": yt,
    }
    # Longer cache → far fewer curator + summary LLM round-trips. The
    # user spent ~$20/day on Anthropic before this knob existed.
    cache.set("brief:response", payload, 120)
    return payload


@app.post("/api/refresh")
async def refresh():
    cache.clear()
    return {"status": "ok"}


def _detect_lang(text: str) -> str:
    """Crude lang detect — Hangul block check is enough for our two langs."""
    if not text:
        return "en"
    for c in text:
        if "가" <= c <= "힣":
            return "ko"
    return "en"


# Lines we don't want polluting reader paragraphs: image markdown that
# trafilatura sometimes returns when an article opens with a hero photo,
# and the various photo-credit / byline-on-its-own-line patterns Korean
# and English outlets use.
#
# Image-markdown regex catches:
#   - standalone lines:      ![alt](url)
#   - lines with prefix:     blah ![alt](url) blah
#   - NESTED brackets in alt: ![소니 PS5 로고. [로이터 = 연합뉴스]](url)
# Inner alt: non-bracket chars OR one level of nested [...]
_IMG_MD_RE = re.compile(
    r"!\[(?:[^\]\[]|\[[^\]]*\])*\]\([^)]+\)"
)
# Raw HTML tags leftover from poor trafilatura parsing (img, figure, span
# with inline style, leftover "border" / "alt" attribute fragments etc.)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Attribute fragments that sometimes leak as plain text when an outlet
# pre-renders its HTML weirdly: e.g. `img border=0 src=…`, `width="600"`,
# `alt="...."` standalone. Drop these.
_ATTR_FRAG_RE = re.compile(
    r"\b(?:img|src|alt|width|height|border|style|class|figure|figcaption)\s*=",
    re.IGNORECASE,
)
_CREDIT_PREFIX_RE = re.compile(
    r"^(?:사진|영상|이미지|일러스트|그래픽|자료(?:사진)?|Photo|Image|Credit|©|기자)\b",
    re.IGNORECASE,
)
_CREDIT_WORDS_RE = re.compile(
    r"\b(?:getty(?:images)?|reuters/|associated\s+press|ap\s+photo|연합뉴스\s*=|뉴시스\s*=)\b",
    re.IGNORECASE,
)
# Numbered/labelled section headers that some Korean outlets paste into
# the body when they ship their own AI summary (매일경제 etc.).
_SECTION_HEADER_RE = re.compile(
    r"^(?:\d+\.\s*)?"
    r"(?:핵심|심층\s*분석|주요\s*경과|다각도\s*분석|결론|요약|용어\s*해설|"
    r"Glossary|Timeline|Key\s*points|Background|Conclusion)",
    re.IGNORECASE,
)
_BRACKETED_HEADER_RE = re.compile(r"^\[[^\]]+\]\s*$")
# Decorative dividers: long runs of box-drawing or separator chars.
# Catches lines like
#   ─────── ⊹ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ Odysseus vers. 1.0 ───────
# Three or more box-drawing chars (U+2500-257F) anywhere in the line
# is a strong signal it's an ASCII-art header, not article body.
_DECORATIVE_DIVIDER_RE = re.compile(r"[─-╿]{3,}|[━═─–—]{4,}")
# Emoji-ish stuff: pictographs, symbols, dingbats, variation selectors.
# Sweeping range — every modern emoji codepoint falls here.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # supplemental symbols, emoticons, pictographs
    "☀-➿"             # misc symbols + dingbats
    "️"                    # variation selector
    "‍"                    # ZWJ used in emoji sequences
    "]+"
)
# U+FFFD (replacement char) means trafilatura got mojibake. Drop the line.
_GARBLE_CHAR = "�"


# Adaptive ceiling on what the reader modal shows. Short articles render
# every paragraph; long ones get clipped where the reader almost certainly
# stops scrolling. Picked from feel — covers ~95% of real articles without
# truncation, keeps the modal from becoming a wall of text.
_PARA_CHAR_BUDGET = 3500   # ≈ 700-800 words of body
_PARA_HARD_MAX = 14        # never more than this many paragraphs


def _split_paragraphs(text: str) -> list[str]:
    """Clean trafilatura's body into reader-friendly paragraphs.
    One consistent rule for every article:
      - strip emojis
      - drop section headers ('1. 심층 분석', '[Glossary]', etc.)
      - drop image markdown + photo credits
      - drop lines with mojibake (U+FFFD)
      - drop very short lines
      - stop after ~3500 characters of body OR 14 paragraphs, whichever
        comes first (short articles still render fully)
    """
    if not text:
        return []
    out: list[str] = []
    total_chars = 0
    for raw in text.split("\n"):
        # Pre-clean pipeline:
        #   1. strip embedded image markdown (rescues the rest of the line)
        #   2. strip raw HTML tags
        #   3. decode HTML entities (`&amp;` → `&`, etc.)
        #   4. strip emojis
        p = _IMG_MD_RE.sub("", raw)
        p = _HTML_TAG_RE.sub("", p)
        p = html.unescape(p)
        p = _EMOJI_RE.sub("", p).strip()
        if not p or len(p) < 30:
            continue
        if _GARBLE_CHAR in p:
            continue
        if _CREDIT_PREFIX_RE.match(p):
            continue
        if _CREDIT_WORDS_RE.search(p):
            continue
        if _SECTION_HEADER_RE.match(p) or _BRACKETED_HEADER_RE.match(p):
            continue
        if _DECORATIVE_DIVIDER_RE.search(p):
            continue
        # Lines that are mostly raw HTML attribute fragments
        if _ATTR_FRAG_RE.search(p) and len(_ATTR_FRAG_RE.findall(p)) >= 2:
            continue
        out.append(p)
        total_chars += len(p)
        if len(out) >= _PARA_HARD_MAX or total_chars >= _PARA_CHAR_BUDGET:
            break
    return out


@app.get("/api/article")
async def article(url: str):
    """Reader-mode view of an external article.

    1. Fetch the page (cached 1d per URL via reader.extract)
    2. trafilatura pulls a clean body + title + image
    3. _split_paragraphs scrubs image-markdown and photo-credit lines
       out of the body — no LLM summarisation, no TL;DR.
    """
    if not url.startswith(("http://", "https://")):
        return {"error": "invalid url"}

    full_key = f"article_reader:{url}"
    cached = cache.get(full_key)
    if cached is not None:
        return cached

    reading = await reader.extract(url)
    if not reading:
        return {"error": "could not extract the article body"}

    # No LLM rewrite — show the publisher's body directly, just cleaned
    # of emojis / section headers / photo credits / mojibake. Cheaper,
    # faster, and the user explicitly asked for the original article
    # ("괜히 에이아이로 돌리지말고 그냥 바로 기사 보여줘").
    result = {
        "url": url,
        "title": html.unescape(reading.title or ""),
        "image": reading.image,
        "byline": reading.byline,
        "excerpt": html.unescape(reading.excerpt or ""),
        "lang": _detect_lang(reading.title or ""),
        "word_count": len(reading.text.split()),
        "paragraphs": _split_paragraphs(reading.text),
    }
    cache.set(full_key, result, 86400)
    return result


@app.get("/api/lab")
async def lab():
    """Lightweight visibility into what the app is doing — counters
    for cached items, agent state, current cache memory footprint.
    Real-time enough for a /lab dashboard refresh every ~5s."""
    store = cache._store
    by_prefix: dict[str, int] = {}
    for k in store.keys():
        p = k.split(":", 1)[0]
        by_prefix[p] = by_prefix.get(p, 0) + 1
    return {
        "cache": {
            "total_keys": len(store),
            "by_prefix": dict(sorted(by_prefix.items(), key=lambda kv: -kv[1])),
        },
        "config": {
            "top_k": getattr(config, "TOP_K", None),
            "per_outlet_limit": getattr(config, "PER_OUTLET_LIMIT", None),
            "feed_cache_ttl": getattr(config, "FEED_CACHE_TTL", None),
            "anthropic_key_set": bool(__import__("os").getenv("ANTHROPIC_API_KEY")),
        },
        "outlets": {
            "configured": len(getattr(config, "OUTLETS", [])),
        },
    }


@app.get("/api/outlets")
async def outlets():
    return {"outlets": config.OUTLETS}


# ── Static frontend ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

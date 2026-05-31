"""FastAPI app: serves API + static frontend."""

from __future__ import annotations

import asyncio
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


# ── Background agent ─────────────────────────────────────────────────
# Runs on a 1-hour cycle. Each tick:
#   1. Clears the source caches so RSS is re-pulled
#   2. Fetches articles (rss.fetch_all already attaches ai_image URLs
#      to any article that doesn't have a real photo)
#   3. Warms the Pollinations CDN for those AI URLs so the browser
#      hits cached images instead of waiting for generation
# Real article images are never inspected or modified here.
AGENT_INTERVAL_SECONDS = 3600


async def _illustrator_agent():
    await asyncio.sleep(20)  # give the app a moment to start serving
    while True:
        try:
            cache.clear()
            articles = await rss.fetch_all()
            # AI URLs are the ones served by Pollinations.ai
            ai_urls = [
                a.image for a in articles
                if a.image and a.image.startswith("https://image.pollinations.ai/")
            ]
            warmed = await illustrator.warm_urls(ai_urls) if ai_urls else 0
            print(
                f"[agent] {len(articles)} articles, "
                f"{len(ai_urls)} AI images, warmed {warmed}",
                flush=True,
            )
        except Exception as exc:
            print(f"[agent] tick failed: {exc}", flush=True)
        await asyncio.sleep(AGENT_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_agent():
    asyncio.create_task(_illustrator_agent())

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/brief")
async def brief():
    """The main payload: ticker tape + mixed feed + sidebars + summary."""
    articles, whale_moves_raw, insider_trades_raw, yt_raw, tape = await asyncio.gather(
        rss.fetch_all(),
        whales.fetch(),
        politicians.fetch(),
        youtube.fetch(),
        prices.fetch_tape(),
    )

    top = await curator.rank(articles, top_k=config.TOP_K)
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

    return {
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
_IMG_MD_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\s*$")
_CREDIT_PREFIX_RE = re.compile(
    r"^(?:사진|영상|이미지|일러스트|그래픽|자료(?:사진)?|Photo|Image|Credit|©|기자)\b",
    re.IGNORECASE,
)
_CREDIT_WORDS_RE = re.compile(
    r"\b(?:getty(?:images)?|reuters/|associated\s+press|ap\s+photo|연합뉴스\s*=|뉴시스\s*=)\b",
    re.IGNORECASE,
)


def _split_paragraphs(text: str) -> list[str]:
    """Turn trafilatura's body text into reader-friendly paragraphs.
    Drops image markdown, photo credits, and one-liner chrome."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.split("\n"):
        p = raw.strip()
        if len(p) < 20:
            continue
        if _IMG_MD_RE.match(p):
            continue
        if _CREDIT_PREFIX_RE.match(p):
            continue
        if _CREDIT_WORDS_RE.search(p):
            continue
        out.append(p)
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

    result = {
        "url": url,
        "title": reading.title,
        "image": reading.image,
        "byline": reading.byline,
        "excerpt": reading.excerpt,
        "lang": _detect_lang(reading.title or ""),
        "word_count": len(reading.text.split()),
        "paragraphs": _split_paragraphs(reading.text),
    }
    cache.set(full_key, result, 86400)
    return result


@app.get("/api/outlets")
async def outlets():
    return {"outlets": config.OUTLETS}


# ── Static frontend ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

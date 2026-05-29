"""FastAPI app: serves API + static frontend."""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import cache, config, mixer, tickers
from backend.agent import curator, summary
from backend.sources import politicians, prices, rss, whales, youtube

load_dotenv()

app = FastAPI(title="dailybrief")

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

    top = await curator.rank(articles, top_k=12)
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
        "profile": {"bio": config.PROFILE.bio, "keywords": config.PROFILE.keywords},
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


@app.get("/api/outlets")
async def outlets():
    return {"outlets": config.OUTLETS}


# ── Static frontend ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

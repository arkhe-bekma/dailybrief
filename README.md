# dailybrief

Personalized daily news app — multi-source aggregator with an LLM curator on top.

## What it pulls

- **Top stories** from ~12 major outlets (BBC, CNN, NYT, WSJ, Bloomberg, Reuters via Google News, Guardian, Al Jazeera, FT, Korea Herald, Yonhap, Chosun)
- **Tech feeds** (The Verge, Ars Technica, TechCrunch, Hacker News)
- **Whale movements** — large on-chain transfers (mocked; wire to Whale Alert or Arkham API)
- **Politician trades** — disclosed congressional/insider trades (mocked; wire to Capitol Trades / QuiverQuant)
- **YouTube strip** — curated channel uploads (mocked; wire to YouTube Data API)

An agent (Claude) re-ranks every article against your interest profile in `backend/config.py` so the top of the page is what *you* care about.

## Stack

- Backend: Python 3.11 + FastAPI + feedparser + httpx + anthropic SDK
- Frontend: vanilla HTML/CSS/JS (no build step)
- Storage: in-memory cache; refresh on demand

## Quick start

```bash
cd ~/Projects/dailybrief
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # edit ANTHROPIC_API_KEY if you want LLM curation
./scripts/run.sh
```

Open http://localhost:8000

## Layout

| Path | Role |
|------|------|
| `backend/main.py` | FastAPI app, serves API + static frontend |
| `backend/config.py` | User interest profile, source list |
| `backend/sources/rss.py` | RSS aggregator (real) |
| `backend/sources/whales.py` | Whale movements (mocked) |
| `backend/sources/politicians.py` | Insider trades (mocked) |
| `backend/sources/youtube.py` | YouTube uploads (mocked) |
| `backend/agent/curator.py` | Claude-based ranker |
| `backend/cache.py` | TTL in-memory cache |
| `frontend/` | index.html + style.css + app.js |

## Roadmap

1. Real Whale Alert / Arkham integration
2. Capitol Trades RSS / QuiverQuant for politician trades
3. YouTube Data API for channel uploads
4. SQLite persistence so the agent can deduplicate across days
5. Daily cron + email/push digest
6. Interest profile editor in the UI

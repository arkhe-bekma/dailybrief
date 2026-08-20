# dailybrief

Personalized daily news reader — a multi-source aggregator with an LLM
curator on top, running at **[dailybrief.fun](https://dailybrief.fun)**.

Articles open in an in-app reader rather than bouncing you to the
publisher. When a publisher blocks the body (paywall, bot wall, dead
link) the reader shows that outlet's own summary plus a link to the
original — it never dead-ends.

## What it pulls

134 configured feeds across 13 categories, defined in
`backend/config.py`:

| Category | Examples |
|---|---|
| world / geo | Reuters, AP, AFP, BBC, Al Jazeera, Nikkei Asia |
| econ / biz | Bloomberg, FT, MarketWatch, HBR |
| tech / ai | The Verge, Ars Technica, TechCrunch, MIT Tech Review |
| science / nature | Nature, Quanta, Mongabay, Guardian Wildlife |
| crypto | Cointelegraph, whale-alert transfers |
| opinion | NYT Opinion, WaPo Opinion, 조선/한경/동아 오피니언 |
| korea / kent | 한겨레, 매일경제, 한국경제, 경향신문, Soompi, 엑스포츠뉴스 |

Outlets with no usable public feed (Reuters, Bloomberg, HBR, hankyung)
route through Google News search and are unwrapped back to the publisher
URL — see *Google News* below.

Also on the page: a price tape, large on-chain transfers, disclosed
politician trades, and a YouTube strip.

## Stack

- **Backend** — Python 3.11 + FastAPI, feedparser, httpx, trafilatura
- **Frontend** — vanilla HTML/CSS/JS, no build step
- **Storage** — SQLite (`backend/data/dailybrief.db`) behind a TTL
  in-memory cache; article bodies are cached on disk so a restart
  doesn't re-pay for extraction
- **LLM** — Anthropic / Gemini for curation, translation, dedup
  (optional; without a key the curator falls back to keyword scoring)
- **Deploy** — Lightsail + systemd + Caddy. See [DEPLOY.md](DEPLOY.md).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # add ANTHROPIC_API_KEY for LLM curation
./scripts/run.sh
```

Open <http://localhost:8000>.

## Layout

| Path | Role |
|------|------|
| `backend/main.py` | FastAPI app — API routes + static frontend |
| `backend/config.py` | Outlet roster, categories, interest profile |
| `backend/db.py` | SQLite: articles, reader bodies, counters, health |
| `backend/cache.py` | TTL in-memory cache |
| `backend/sources/rss.py` | Feed aggregation + Google News unwrapping |
| `backend/agent/reader.py` | Article body extraction (trafilatura) |
| `backend/agent/curator.py` | LLM ranker |
| `backend/agent/ranker.py` | Cross-outlet dedup + scoring |
| `backend/agent/translator.py` | KO ⇄ EN article translation |
| `frontend/` | `index.html`, `app.js`, `style.css` |
| `frontend/status.*` | Public status page |
| `scripts/Caddyfile.production` | The live edge config |

## Pages

| Route | What |
|---|---|
| `/` | The feed |
| `/status` | Public: per-source article-extraction health |
| `/lab` | Admin dashboard |
| `/account`, `/login` | Auth |
| `/pro` | **Closed** — 302s to `/`. See DEPLOY.md to re-open. |

## API

| Endpoint | What |
|---|---|
| `GET /api/brief` | The mixed feed + tape + strips |
| `GET /api/page?n=&cat=` | Paginated archive, card-shaped |
| `GET /api/search?q=` | Search headlines / deks / outlets |
| `GET /api/article?url=` | Reader body for one article |
| `GET /api/translate?url=&lang=` | KO ⇄ EN translation |
| `GET /api/health` | Liveness |
| `GET /api/health/extraction?hours=` | Per-source reader success rate |
| `GET /api/health/feeds` | Probe every feed (admin, uncached) |

## Google News

Several outlets are only reachable through Google News search feeds.
Google retired the old interstitial in 2025: the article id is now an
encrypted blob, the shell page carries no canonical/meta-refresh to
scrape, and requesting it without `?oc=5` returns 400.

`rss._gnews_rpc_resolve` fetches the shell with `?oc=5`, reads
`data-n-a-sg` / `data-n-a-ts` out of it, and posts them to Google's own
`Fbv4je` batchexecute RPC, which returns the publisher URL. If Google
changes this again, `/status` is where it will show up first — the
affected outlets drop to a 0% success rate.

## Operating it

- **Never edit files directly on the server.** Push to `main`; the box
  syncs within a minute. See DEPLOY.md.
- `/status` answers "why won't articles open" without a login.
- Paywalled sources showing 0% is expected, not a regression — those
  articles serve the publisher's summary plus a link out.

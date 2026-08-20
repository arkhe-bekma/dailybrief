# dailybrief

Personalized daily news reader — a multi-source aggregator with an LLM
curator on top, running at **[dailybrief.fun](https://dailybrief.fun)**.

Articles open in an in-app reader rather than bouncing you to the
publisher. **If there is no article body, the story is not listed.** A
headline with a two-line blurb under it is not an article, so stories
whose body we cannot show are dropped at ingest, swept out of the feed
every 25 minutes, and removed on sight if one slips through.

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

Outlets with no usable public feed (HBR, hankyung, K-ent) route through
Google News search and are unwrapped back to the publisher URL — see
*Google News* below. Pin such queries to the publisher's **article
path** (`site:hankyung.com/article`): a bare `site:` or section-path
query makes Google return paginated section indexes, which extract
cleanly as twenty paragraphs of prose and are only detectable by URL.

`config.RETIRED_OUTLETS` holds 24 outlets removed because they never
yield a readable body — hard paywalls (NYT, Reuters, Bloomberg, FT,
Economist, MarketWatch) and sites that render the body in JS (조선일보,
JTBC). Each was measured, not assumed. Restoring one is a single line.

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
| `POST /api/admin/sweep-bodies` | Drop active articles with no body |

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
- A source at 0% means its articles are being dropped, not shown badly.
  Check whether it belongs in `RETIRED_OUTLETS`.
- Adding an outlet? Verify it extracts first — fetch its feed, run
  `reader.extract_detailed` over four articles, and only add it if they
  come back with a body. Every outlet added this way was measured.

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

from backend import cache, config, db, mixer, tickers
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
    import time as _t
    while True:
        started = _t.time()
        ok = True
        note = ""
        try:
            # Drop the feed-level cache. Per-URL probe caches stay
            # (24h success, 1h fail) so we don't re-fetch known-good URLs.
            dropped = 0
            for k in list(cache._store.keys()):
                if k.startswith("rss:") or k.startswith("article_reader:") or k == "brief:response":
                    cache._store.pop(k, None)
                    dropped += 1
            note = f"dropped {dropped} cache keys"
            print(f"[agent] refresh cycle: {note}", flush=True)
        except Exception as exc:
            ok = False
            note = str(exc)[:200]
            print(f"[agent] tick failed: {exc}", flush=True)
        try:
            await db.log_agent_run("refresh", started, _t.time(), ok, note)
        except Exception:
            pass

        # User-tunable interval (persisted in DB via /api/lab/settings).
        interval = await db.get_setting(
            "agent_interval_seconds", AGENT_INTERVAL_SECONDS
        )
        await asyncio.sleep(max(60, int(interval)))


@app.on_event("startup")
async def _start():
    await db.init()
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

    # Persist what we just decided to show. Survives restart and
    # powers the /lab dashboard counts.
    try:
        rows = [
            {
                "url": m.get("url"),
                "title": m.get("title"),
                "image": m.get("image"),
                "outlet": m.get("outlet"),
                "category": m.get("category"),
                "lang": m.get("lang"),
                "summary": m.get("dek") or "",
                "score": m.get("score") or 0,
                "published_at": m.get("ts"),
            }
            for m in mixed if m.get("kind") == "news" and m.get("url")
        ]
        await db.upsert_articles(rows)
        await db.bump_counter("articles_ingested", by=len(rows))

        # Tell the frontend how deep the archive goes so its pager can
        # render however many pages the DB actually supports.
        payload["db_total_articles"] = await db.count_articles()
    except Exception as exc:
        print(f"[db] upsert articles failed: {exc}")

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


def _scrub_html_artifacts(line: str) -> str:
    """Two-pass strip so partial / encoded HTML can't slip through:
      1. strip raw <…>
      2. unescape  (turns &lt;img&gt; into <img>)
      3. strip <…> AGAIN
      4. unescape once more
    Then also kill any obvious bare-tag fragments like `<img border` that
    were left dangling because the closing `>` was on another line.
    """
    s = _HTML_TAG_RE.sub("", line)
    s = html.unescape(s)
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    # Kill orphan opening tags: `<img …` `<figure …` etc that lost their `>`
    s = re.sub(r"<\s*(img|figure|figcaption|span|div|p|br|table|tr|td|a|iframe|script|style)\b[^<]*",
               "", s, flags=re.IGNORECASE)
    return s


def _split_paragraphs(text: str) -> list[str]:
    """Clean trafilatura's body into reader-friendly paragraphs.
    One consistent rule for every article:
      - strip emojis
      - drop section headers ('1. 심층 분석', '[Glossary]', etc.)
      - drop image markdown + photo credits
      - drop lines with mojibake (U+FFFD)
      - drop very short lines
      - drop HTML artifacts (<img border …> partial tags etc.)
      - stop after ~3500 characters of body OR 14 paragraphs, whichever
        comes first (short articles still render fully)
    """
    if not text:
        return []
    out: list[str] = []
    total_chars = 0
    for raw in text.split("\n"):
        p = _IMG_MD_RE.sub("", raw)
        p = _scrub_html_artifacts(p)
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
        await db.bump_counter("reader_cache_hits_mem")
        return cached

    # SQLite second-level cache — survives restart. Keeps trafilatura
    # work that's already been paid for.
    saved = await db.get_reader(url)
    if saved is not None:
        cache.set(full_key, saved, 86400)
        await db.bump_counter("reader_cache_hits_disk")
        return saved

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
    # Persist so a restart doesn't drop the scraped body.
    await db.save_reader(url, result)
    await db.bump_counter("reader_extracts_ok")
    return result


# ── /api/lab — dashboard data ────────────────────────────────────────
@app.get("/api/lab")
async def lab_overview():
    """Snapshot for the /lab dashboard: cache footprint, config, DB
    counters, outlet roster. Polled every few seconds."""
    store = cache._store
    by_prefix: dict[str, int] = {}
    for k in store.keys():
        p = k.split(":", 1)[0]
        by_prefix[p] = by_prefix.get(p, 0) + 1
    db_stats = await db.stats()
    agent_interval = await db.get_setting(
        "agent_interval_seconds", AGENT_INTERVAL_SECONDS
    )
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
            "agent_interval_seconds": agent_interval,
        },
        "outlets_configured": len(getattr(config, "OUTLETS", [])),
        "db": db_stats,
    }


@app.get("/api/lab/agent-runs")
async def lab_agent_runs(limit: int = 50):
    return {"runs": await db.recent_agent_runs(limit=min(max(limit, 1), 200))}


@app.get("/api/lab/settings")
async def lab_settings_get():
    return {
        "agent_interval_seconds": await db.get_setting(
            "agent_interval_seconds", AGENT_INTERVAL_SECONDS
        ),
    }


@app.post("/api/lab/settings")
async def lab_settings_set(body: dict):
    """Persist user-tunable knobs. The refresh agent reads these on
    each tick so changes apply within one cycle."""
    out: dict = {}
    if "agent_interval_seconds" in body:
        v = int(body["agent_interval_seconds"])
        v = max(60, min(v, 6 * 3600))   # clamp 1 min … 6 h
        await db.set_setting("agent_interval_seconds", v)
        out["agent_interval_seconds"] = v
    return out


@app.get("/lab")
async def lab_page():
    return FileResponse(FRONTEND_DIR / "lab.html")


@app.get("/api/page")
async def api_page(n: int = 1, size: int = 31, cat: str | None = None):
    """Lazy pagination — page 2+ pulls from the SQLite archive instead
    of the in-memory curator output. Total page count grows as the DB
    grows, with no upper limit baked in."""
    n = max(1, n)
    size = max(1, min(size, 100))
    cat_clean = cat if cat and cat != "all" else None
    total = await db.count_articles(cat=cat_clean)
    pages = max(1, (total + size - 1) // size)
    n = min(n, pages)
    items = await db.list_articles(
        offset=(n - 1) * size,
        limit=size,
        cat=cat_clean,
    )
    converted = [
        {
            "kind": "news",
            "url": r.get("url"),
            "title": r.get("title"),
            "image": r.get("image"),
            "outlet": r.get("outlet"),
            "category": r.get("category"),
            "lang": r.get("lang"),
            "dek": r.get("summary"),
            "ts": r.get("published_at"),
            "score": r.get("score") or 0,
            "tickers": [], "sparks": {},
        }
        for r in items
    ]
    return {"page": n, "size": size, "total_pages": pages, "total_items": total, "items": converted}


@app.get("/api/outlets")
async def outlets():
    return {"outlets": config.OUTLETS}


# ── Static frontend ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

"""FastAPI app: serves API + static frontend."""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import auth, cache, config, db, mixer, tickers
from backend.agent import (
    curator, dedup, illustrator, ranker, reader, registry, sorter, summary,
    translator, validator,
)
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
    # Wrap startup so a single migration / schedule failure can never
    # take the whole service down (which is what Caddy reports as 502).
    try:
        await db.init()
    except Exception as exc:
        print(f"[startup] db.init failed: {exc!r}", flush=True)
    try:
        await auth.init()  # creates users/sessions tables + seeds admin/admin
    except Exception as exc:
        print(f"[startup] auth.init failed: {exc!r}", flush=True)
    # Idempotent repair: drop image values that aren't absolute URLs, so
    # cards stop requesting publisher photos from our own domain.
    try:
        fixed = await db.repair_bad_images()
        if fixed:
            print(f"[startup] cleared {fixed} non-absolute article images", flush=True)
    except Exception as exc:
        print(f"[startup] repair_bad_images failed: {exc!r}", flush=True)
    # Restore any API keys the admin previously set via the settings
    # popover. Settings rows survive restarts; os.environ does not.
    try:
        import os as _os
        for _spec in _KEY_PROVIDERS.values():
            _stored = await db.get_setting(_spec["env"], "")
            if _stored and not _os.environ.get(_spec["env"]):
                _os.environ[_spec["env"]] = _stored
                print(f"[startup] restored {_spec['env']} from settings", flush=True)
    except Exception as exc:
        print(f"[startup] key restore failed: {exc!r}", flush=True)
    # ── Background workers (minimal set) ────────────────────────────
    # The Lightsail 512MB box was under sustained CPU + SQLite-lock
    # pressure from 5 concurrent workers. The site was technically up
    # but /api/brief was taking 5-8 s. Strict diet now: only the two
    # genuinely cheap workers stay enabled. Heavy ones (validation,
    # resummary, prune) are admin-triggered via the lab when needed.
    try:
        asyncio.create_task(_refresh_agent())   # hourly cache-bust, very cheap
    except Exception as exc:
        print(f"[startup] refresh agent failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_archive_worker())  # 6-hourly, single UPDATE stmt
    except Exception as exc:
        print(f"[startup] archive worker failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_rss_ingest_worker())  # 30-min light RSS-only fetch
    except Exception as exc:
        print(f"[startup] rss ingest worker failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_brief_refresh_worker())  # hourly cache refill
    except Exception as exc:
        print(f"[startup] brief refresh worker failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_ranker_worker())   # 20-min importance + dedup pass
    except Exception as exc:
        print(f"[startup] ranker worker failed to schedule: {exc!r}", flush=True)
    # Prewarm the cache immediately so the very first visitor doesn't
    # see an empty payload.
    try:
        async def _prewarm():
            await asyncio.sleep(15)
            try:
                fast = _brief_db_fallback()
                if fast is not None:
                    fast = _polish_mixed(fast)
                    cache.set("brief:response", fast, BRIEF_CACHE_TTL)
                    cache.set("brief:response_stale", fast, BRIEF_STALE_GRACE)
                    print("[startup] brief cache prewarmed (fast path)", flush=True)
            except Exception as exc:
                print(f"[startup] brief prewarm failed: {exc!r}", flush=True)
        asyncio.create_task(_prewarm())
    except Exception as exc:
        print(f"[startup] prewarm failed to schedule: {exc!r}", flush=True)
    # DISABLED workers (admin-triggered via lab → ACTIONS):
    #   _validation_worker   – per-article HTTP fetch every 5 s, network-heavy
    #   _resummary_worker    – DB read+rewrite loop
    #   _prune_worker        – once-a-day VACUUM, OK to skip on small box


# ── Bad-title detection ─────────────────────────────────────────────
# trafilatura sometimes pulls a sidebar widget label as the page title
# when the actual <title>/<h1> isn't where it expects. Common offenders:
# "Editor's choice", "Latest News", "Top stories", "Most read", "More
# from <outlet>", category-name-only labels, etc. When the extracted
# title matches one of these, fall back to the RSS-supplied title.
_BAD_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"editor[''’]?s?\s+choice"
    r"|editor[''’]?s?\s+pick(?:s)?"
    r"|latest\s+news"
    r"|breaking\s+news"
    r"|top\s+stor(?:y|ies)"
    r"|most\s+(?:read|popular|viewed)"
    r"|more\s+from\b.*"
    r"|recommended\s+(?:reads?|for\s+you)"
    r"|trending(?:\s+now)?"
    r"|featured(?:\s+stor(?:y|ies))?"
    r"|popular\s+(?:now|today|stories)"
    r"|today[''’]?s?\s+(?:pick|top)\w*"
    r"|news(?:letter)?$"
    r"|home\s*$"
    r"|404\b.*"
    r"|page\s+not\s+found"
    r"|access\s+denied"
    r"|sign\s+in\b.*"
    r"|log\s+in\b.*"
    # Korean common boilerplate labels
    r"|편집자\s*추천"
    r"|많이\s*본\s*뉴스"
    r"|주요\s*뉴스"
    r"|최신\s*뉴스"
    r"|인기\s*기사"
    r")\s*$",
    re.IGNORECASE,
)


def _is_bad_title(title: str) -> bool:
    """True if `title` looks like a generic page-furniture label rather
    than a real article headline."""
    if not title:
        return True
    t = title.strip()
    if len(t) < 10:
        return True
    if _BAD_TITLE_RE.match(t):
        return True
    return False


def _better_title(extracted: str, fallback: str) -> str:
    """Choose the better headline. If trafilatura's extracted title is
    generic boilerplate (sidebar widget label) but the RSS fallback is
    real, use the fallback. Otherwise prefer the extracted one (which
    is usually more canonical than the publisher's RSS title)."""
    e = (extracted or "").strip()
    f = (fallback or "").strip()
    if not e and f:
        return f
    if e and _is_bad_title(e) and f and not _is_bad_title(f):
        return f
    return e or f


# ── Resummary worker ────────────────────────────────────────────────
# Walks every article whose stored summary is shorter than what the
# extracted body would yield, and force-overwrites the summary with
# the real body's first 2 paragraphs. User explicitly: no AI rewrites,
# no publisher one-liners — the dek under each title must be the
# actual article content, just like 서울신문 / 일간스포츠 already show.
def _dek_cap(lang: str | None) -> int:
    """Per-language cap on the card dek length. Korean glyphs render
    roughly 1.8× wider per character than Latin in Pretendard, so a
    shorter character cap on Korean produces a visually-similar block
    to a longer cap on English. Picked so a Korean dek and English dek
    occupy comparable card heights — user complaint: 한글 기사가 한바가지,
    영어 기사는 너무 짧다 → meet in the middle."""
    return 240 if (lang or "en").lower().startswith("ko") else 440


def _compose_dek_from_body(
    paras: list[str], title: str = "", lang: str | None = None,
) -> str:
    """Pick the strongest first chunk of real prose out of an article
    body, capped at a language-appropriate length so KO and EN cards
    feel visually similar. Skips title-as-paragraph + very short header
    lines (subtitles, captions) that publishers place ahead of the lede.
    """
    cap = _dek_cap(lang)
    t = (title or "").strip().lower()
    picked: list[str] = []
    total = 0
    # Korean publishers sometimes pack the whole lede into one dense
    # paragraph — pulling 2 of those would blow well past the cap.
    # English outlets need 1-2 paragraphs to reach a comparable dek.
    max_paras = 1 if (lang or "en").lower().startswith("ko") else 2
    for raw in paras:
        s = (raw or "").strip()
        if not s:
            continue
        # Related-links tail markers — once we cross into them, the
        # rest of the body is junk navigation; stop walking.
        if _looks_like_related_link(s):
            break
        norm = s.lower()
        # Skip title-as-paragraph (exact match or near-exact).
        if t and (norm == t or (norm.startswith(t) and len(norm) <= len(t) + 4)):
            continue
        # Skip header/subtitle lines under 80 chars — too short to
        # carry meaningful body text.
        if len(s) < 80:
            continue
        picked.append(s)
        total += len(s) + 1
        if total >= cap or len(picked) >= max_paras:
            break
    return (" ".join(picked))[:cap]


async def _process_resummary_batch(limit: int) -> tuple[int, int]:
    """Single resummary pass — pull `limit` candidates and rewrite
    their deks from extracted body. Returns (rewritten, total)."""
    try:
        batch = await db.list_resummary_candidates(limit=limit)
    except Exception as exc:
        print(f"[resummary] queue read failed: {exc!r}", flush=True)
        return 0, 0
    rewritten = 0
    for row in batch:
        url = row.get("url")
        if not url:
            continue
        try:
            payload = await db.get_reader(url)
            if not payload:
                continue
            paras = payload.get("paragraphs") or []
            if not paras:
                continue
            title = payload.get("title") or row.get("title") or ""
            lang = row.get("lang") or payload.get("lang")
            combined = _compose_dek_from_body(paras, title, lang)
            if not combined or len(combined) <= row.get("summary_len", 0):
                continue
            await db.force_update_summary(url, combined)
            rewritten += 1
        except Exception as exc:
            print(f"[resummary] {url[:60]} failed: {exc!r}", flush=True)
    return rewritten, len(batch)


async def _resummary_worker():
    """Two phases:
      • Startup burst — chew through whatever's in the queue right now
        in big batches with no sleep between, so a fresh deploy clears
        the backlog in well under a minute.
      • Steady state — 30-row batches every second; sleep longer when
        the queue is empty.
    User asked for "20-30 at a time" + "fix the visible feed fast"."""
    await asyncio.sleep(20)
    print("[resummary] worker armed — entering startup burst", flush=True)

    # Phase 1: burst until the queue is empty or we've done 60 batches.
    for _ in range(60):
        rewritten, total = await _process_resummary_batch(limit=40)
        if rewritten:
            print(f"[resummary] burst rewrote {rewritten}/{total}", flush=True)
        if total < 40:
            break
        await asyncio.sleep(0.2)

    print("[resummary] burst complete — entering steady cadence", flush=True)

    # Phase 2: steady cadence.
    while True:
        rewritten, total = await _process_resummary_batch(limit=30)
        if rewritten:
            print(f"[resummary] rewrote {rewritten}/{total}", flush=True)
        if total == 0:
            await asyncio.sleep(60)
        else:
            await asyncio.sleep(1)


async def _rss_ingest_worker():
    """LIGHTWEIGHT periodic RSS ingest. Runs every 30 min and ONLY
    does: fetch every RSS feed → drop blocked URLs → upsert into the
    articles table. No curator, no LLM, no image scrape — those are
    what made _brief_build_full time out at 60 s.

    This guarantees the DB stays populated with fresh articles even
    when the heavy curator chain fails. The fast-path query
    (_brief_db_fallback) then has new rows to serve on the next
    /api/brief tick.
    """
    INTERVAL_SECONDS = 30 * 60   # 30 min
    await asyncio.sleep(60)
    while True:
        try:
            articles = await asyncio.wait_for(rss.fetch_all(), timeout=120.0)
            blocked = await db.blocked_url_set()
            if blocked:
                articles = [a for a in articles if a.url not in blocked]
            if articles:
                now_ts = int(__import__("time").time())
                rows = [{
                    "url": a.url, "title": a.title, "image": a.image,
                    "outlet": a.outlet, "category": a.category, "lang": a.lang,
                    "summary": a.summary, "published_at": a.published,
                    "fetched_at": now_ts,
                } for a in articles if a.url]
                await db.upsert_articles(rows)
                # Drop the brief response cache so the next /api/brief
                # request runs the fast path and picks up the new rows.
                cache._store.pop("brief:response", None)
                print(
                    f"[rss-ingest] pulled {len(articles)} articles, "
                    f"upserted {len(rows)} rows",
                    flush=True,
                )
        except asyncio.TimeoutError:
            print("[rss-ingest] timed out at 120 s", flush=True)
        except Exception as exc:
            print(f"[rss-ingest] failed: {exc!r}", flush=True)
        await asyncio.sleep(INTERVAL_SECONDS)


async def _brief_refresh_worker():
    """ONE controlled brief rebuild every 20 min. Replaces the
    per-request asyncio.create_task pattern that was OOM-killing
    uvicorn under traffic. Uses the hard 60 s timeout inside
    _kickoff_brief_rebuild so a stuck call can't pin the flag.
    """
    REFRESH_EVERY = 60 * 60  # 1 hour — slower so the box has more idle CPU
    # Wait a long time before first run so startup is fully settled.
    await asyncio.sleep(90)
    while True:
        try:
            await _kickoff_brief_rebuild()
        except Exception as exc:
            print(f"[brief-refresh] {exc!r}", flush=True)
        await asyncio.sleep(REFRESH_EVERY)


async def _archive_worker():
    """6-hourly auto-archive: anything older than 7 days flips to
    archived = 1. Articles aren't deleted — operator can still pull
    them back from /api/admin/unarchive-all. Was hourly + cleared the
    ENTIRE cache on every run, which forced a full cold rebuild for
    the next visitor. Now: 6h cadence + targeted cache key drop so
    only the brief response is invalidated."""
    ARCHIVE_DAYS = 7
    INTERVAL_SECONDS = 6 * 3600
    await asyncio.sleep(120)  # let startup settle properly
    while True:
        try:
            moved = await db.archive_old_articles(days_old=ARCHIVE_DAYS)
            if moved:
                stats = await db.archive_stats()
                # Targeted invalidation: only the public-facing brief
                # cache, NOT the RSS feed cache or per-URL caches.
                cache._store.pop("brief:response", None)
                print(
                    f"[archive] moved {moved} → archive. "
                    f"active={stats['active']} archived={stats['archived']}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[archive] failed: {exc!r}", flush=True)
        await asyncio.sleep(INTERVAL_SECONDS)


async def _ranker_worker():
    """The ranking department. Every 20 min it re-scores the recent
    active pool: tags outlet authority, clusters same-story-across-outlets,
    collapses each cluster to one, and writes an importance score driven
    by authority + corroboration + slow recency. The feed serving path
    already orders by (premium, score) and hides dup_of rows, so this is
    what makes the wall read like a real front page. Runs in a thread
    (rank_active is sync + CPU-bound, ~1.5s over 6k rows) so it never
    blocks the event loop. First pass runs soon after boot."""
    INTERVAL_SECONDS = 20 * 60
    await asyncio.sleep(50)   # let startup settle
    print("[ranker] department armed", flush=True)
    while True:
        try:
            stats = await asyncio.to_thread(ranker.rank_active)
            # New scores / collapses → refresh the public feed cache.
            cache._store.pop("brief:response", None)
            print(
                f"[ranker] examined={stats['examined']} "
                f"clusters={stats['clusters_multi']} "
                f"collapsed={stats['collapsed_dupes']} "
                f"took={stats['took_s']}s",
                flush=True,
            )
        except Exception as exc:
            print(f"[ranker] pass failed: {exc!r}", flush=True)
        await asyncio.sleep(INTERVAL_SECONDS)


async def _prune_worker():
    """Daily maintenance: drops articles older than 60 days, caps the
    reader_results cache at 6000 rows, VACUUMs the file to actually
    reclaim disk. Survives forever — sleeps 24h between runs."""
    await asyncio.sleep(120)  # let the rest of startup settle
    while True:
        try:
            result = await db.prune_once()
            await db.set_setting("last_prune", result)
            removed = result["removed_articles"] + result["removed_reader"]
            mb = result["db_bytes_after"] / 1e6
            print(
                f"[prune] removed {removed} rows, DB now {mb:.1f}MB "
                f"({result['removed_articles']} articles + "
                f"{result['removed_reader']} reader bodies)",
                flush=True,
            )
        except Exception as exc:
            print(f"[prune] failed: {exc!r}", flush=True)
        await asyncio.sleep(24 * 3600)


# ── Validation worker ────────────────────────────────────────────────
# Picks unvalidated articles from the DB in small batches, runs each
# through reader.extract → validator.validate, and updates the DB.
# Articles that pass have their body persisted to reader_results so
# the publisher URL going dead later can't destroy our copy.
#
# Runs as a background task — never blocks /api/brief. Lab page polls
# /api/ingest/status for the live progress dashboard.
_INGEST_BATCH = 8
_INGEST_PAUSE_BUSY = 1.5
_INGEST_PAUSE_IDLE = 60.0   # seconds between polls when queue is empty (was 30)


async def _validate_one(article: dict) -> tuple[int, str]:
    """Process a single article. Returns (validated_status, reason)."""
    url = article.get("url")
    if not url:
        return -1, "no-url"

    # 1. Try the cached reader body first — every article a user has
    # opened in the modal already lives in reader_results.
    body_payload = await db.get_reader(url)
    if not body_payload:
        # 2. Pull the body fresh.
        try:
            reading = await reader.extract(url)
        except Exception as exc:
            try:
                await db.delete_article_by_user(url, reason="extract-error", note=type(exc).__name__)
            except Exception:
                pass
            return -1, f"extract-error:{type(exc).__name__}"
        if not reading or not (reading.text or reading.excerpt):
            try:
                await db.delete_article_by_user(url, reason="extract-failed")
            except Exception:
                pass
            return -1, "extract-failed"
        body_payload = {
            "url": url,
            "title": reading.title or article.get("title") or "",
            "image": reading.image,
            "byline": reading.byline,
            "excerpt": reading.excerpt or "",
            "lang": article.get("lang") or _detect_lang(reading.title or ""),
            "word_count": len((reading.text or "").split()),
            "paragraphs": _split_paragraphs(reading.text or ""),
        }
        try:
            await db.save_reader(url, body_payload)
        except Exception as exc:
            print(f"[ingest] save_reader failed for {url[:60]}: {exc!r}", flush=True)

    # 3. Run the validator.
    passed, reason = validator.validate(article, body_payload)
    if passed:
        # Backfill the card dek from the extracted body. Uses the
        # smart composer that skips title-as-paragraph + subtitle
        # stubs so EN cards match KR density.
        combined = _compose_dek_from_body(
            body_payload.get("paragraphs") or [],
            article.get("title") or "",
            article.get("lang") or body_payload.get("lang"),
        )
        if combined:
            try:
                await db.update_article_summary(url, combined)
            except Exception as exc:
                print(f"[ingest] summary update failed: {exc!r}", flush=True)
        # If the article didn't have an image but extraction found one,
        # stamp it onto the articles row.
        body_image = body_payload.get("image")
        if body_image and not article.get("image"):
            try:
                await db.update_article_image(url, body_image)
            except Exception as exc:
                print(f"[ingest] image update failed: {exc!r}", flush=True)
        return 1, "ok"
    return -1, reason


async def _validation_worker():
    """Long-running loop. Sleeps cheaply when there's nothing to do."""
    await asyncio.sleep(8)  # let the rest of startup settle
    print("[ingest] validation worker armed", flush=True)
    while True:
        try:
            batch = await db.list_unvalidated(limit=_INGEST_BATCH)
        except Exception as exc:
            print(f"[ingest] queue read failed: {exc!r}", flush=True)
            await asyncio.sleep(_INGEST_PAUSE_IDLE)
            continue
        if not batch:
            await asyncio.sleep(_INGEST_PAUSE_IDLE)
            continue
        # Process the batch in parallel — bounded by _INGEST_BATCH so we
        # don't overrun trafilatura's HTTP fetches.
        results = await asyncio.gather(
            *[_validate_one(a) for a in batch],
            return_exceptions=True,
        )
        for art, res in zip(batch, results):
            url = art.get("url")
            if not url:
                continue
            if isinstance(res, Exception):
                status, reason = -1, f"crash:{type(res).__name__}"
            else:
                status, reason = res
            try:
                if status == -1 and reason and reason.startswith("extract"):
                    # Row was already deleted by _validate_one; just count it.
                    pass
                else:
                    await db.set_article_validated(url, status, reason)
                await db.bump_counter(
                    "ingest_pass" if status == 1 else "ingest_fail",
                )
            except Exception as exc:
                print(f"[ingest] mark failed for {url[:60]}: {exc!r}", flush=True)
        await asyncio.sleep(_INGEST_PAUSE_BUSY)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/translate")
async def api_translate(url: str, lang: str = "ko"):
    """Translate an article into `lang`. Pulls the reader payload from
    the SQLite cache (so the user must have opened the article at
    least once via /api/article — usually true since the reader modal
    opens the translation request)."""
    if not url.startswith(("http://", "https://")):
        return {"error": "invalid url"}
    payload = await db.get_reader(url)
    if not payload:
        # Lazily fetch the reader body before translating, so the
        # translate button works the first time on any article too.
        reading = await reader.extract(url)
        if not reading:
            return {"error": "could not extract the article body"}
        payload = {
            "url": url,
            "title": reading.title or "",
            "image": reading.image,
            "byline": reading.byline,
            "excerpt": reading.excerpt or "",
            "lang": _detect_lang(reading.title or ""),
            "word_count": len((reading.text or "").split()),
            "paragraphs": _split_paragraphs(reading.text or ""),
        }
        await db.save_reader(url, payload)

    out = await translator.translate(payload, target_lang=lang)
    if out is None:
        return {"error": "translation unavailable — check GEMINI_API_KEY or model error"}
    return out


@app.post("/api/ingest/resummary")
async def trigger_resummary(request: Request, limit: int = 200):
    """Force-rerun the dek rewriter on `limit` short-summary articles
    right now. Useful for clearing a freshly-noticed backlog without
    waiting for the worker's pace. Returns counts. Admin-only."""
    await _require_admin(request)
    rewritten, total = await _process_resummary_batch(limit=max(1, min(limit, 500)))
    return {"rewritten": rewritten, "examined": total}


def _mask_api_key(k: str | None) -> str | None:
    """Return a masked fingerprint of an API key (first 4 · last 4)
    so the operator can see which key is active without exposing the
    full secret. None when the env var isn't set."""
    if not k or len(k) < 10:
        return None
    return f"{k[:4]}…{k[-4:]}"


# ── API key management ──────────────────────────────────────────────
# Admin enters a single key in the settings popover; the server detects
# the provider from the key prefix, validates against the provider's
# /models endpoint, persists into the SQLite settings table, and updates
# os.environ so every subsequent agent call sees the new key without
# requiring a restart. Settings rows are restored to os.environ on
# startup (see _start). NB: keys never leave the server — the frontend
# only ever receives the masked fingerprint.

_KEY_PROVIDERS = {
    "anthropic": {
        "env": "ANTHROPIC_API_KEY",
        "label": "Anthropic (Claude)",
        "prefix": "sk-ant-",
        "validate_url": "https://api.anthropic.com/v1/models",
        "headers": lambda k: {
            "x-api-key": k,
            "anthropic-version": "2023-06-01",
        },
        "url_with_key": False,
        "models_path": ("data",),    # response["data"] is the list
    },
    "gemini": {
        "env": "GEMINI_API_KEY",
        "label": "Google Gemini",
        "prefix": "AIza",
        "validate_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "headers": lambda k: {},
        "url_with_key": True,        # query-param auth
        "models_path": ("models",),
    },
}


def _detect_provider(key: str) -> str | None:
    """Pattern-match the key prefix back to a provider. Returns None for
    anything we don't recognize so the caller can surface a clean 400."""
    k = (key or "").strip()
    for name, spec in _KEY_PROVIDERS.items():
        if k.startswith(spec["prefix"]):
            return name
    return None


async def _validate_provider_key(provider: str, key: str) -> tuple[bool, list[str], str | None]:
    """Hit the provider's models endpoint with the candidate key. Returns
    (ok, list-of-model-ids, error-message). Treats anything other than a
    2xx with a parseable models array as a failure."""
    import httpx as _httpx
    spec = _KEY_PROVIDERS[provider]
    url = spec["validate_url"]
    if spec["url_with_key"]:
        url = f"{url}?key={key}"
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=spec["headers"](key))
    except Exception as exc:
        return False, [], f"network error: {type(exc).__name__}"
    if r.status_code != 200:
        # Surface the provider's own error reason when available.
        try:
            detail = r.json()
            msg = (
                (detail.get("error") or {}).get("message")
                if isinstance(detail.get("error"), dict)
                else None
            ) or r.text[:160]
        except Exception:
            msg = r.text[:160] or f"HTTP {r.status_code}"
        return False, [], msg
    try:
        body = r.json()
    except Exception:
        return False, [], "non-JSON response"
    # Walk to the models list (each provider names it differently).
    node = body
    for k in spec["models_path"]:
        node = (node or {}).get(k)
        if node is None:
            return False, [], "unexpected response shape"
    models: list[str] = []
    for m in node:
        mid = (m.get("id") or m.get("name") or "").strip()
        # Gemini returns "models/gemini-2.5-flash" — strip the prefix.
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if mid:
            models.append(mid)
    return True, models, None


@app.get("/api/admin/keys")
async def admin_list_keys(request: Request):
    """Returns the currently configured providers with masked
    fingerprints. Admin-only — keys themselves never leave the server."""
    await _require_admin(request)
    import os as _os
    out = {}
    for name, spec in _KEY_PROVIDERS.items():
        env_var = spec["env"]
        val = _os.environ.get(env_var) or ""
        # Gemini accepts GOOGLE_API_KEY as a fallback (used by some
        # Google-side examples); surface it if that's all we've got.
        if not val and name == "gemini":
            val = _os.environ.get("GOOGLE_API_KEY") or ""
        out[name] = {
            "label": spec["label"],
            "set": bool(val),
            "masked": _mask_api_key(val) if val else None,
        }
    return out


@app.post("/api/admin/keys")
async def admin_set_key(request: Request, body: dict):
    """Single-input endpoint: takes any provider's key, detects which
    one it belongs to, validates against /models, and on success
    persists + applies. Returns the provider label + masked fingerprint
    + the list of model IDs the provider says this key can use."""
    await _require_admin(request)
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    provider = _detect_provider(key)
    if not provider:
        raise HTTPException(
            status_code=400,
            detail="key shape doesn't match any known provider "
                   "(Anthropic keys start with 'sk-ant-', Gemini keys with 'AIza')",
        )
    ok, models, err = await _validate_provider_key(provider, key)
    if not ok:
        raise HTTPException(status_code=400, detail=f"validation failed: {err}")
    spec = _KEY_PROVIDERS[provider]
    # Persist to settings table (survives restarts) + apply to live env.
    try:
        await db.set_setting(spec["env"], key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"persist failed: {exc!r}")
    import os as _os
    _os.environ[spec["env"]] = key
    return {
        "provider": provider,
        "label": spec["label"],
        "masked": _mask_api_key(key),
        "models": models,
    }


@app.delete("/api/admin/keys/{provider}")
async def admin_delete_key(provider: str, request: Request):
    """Wipe a provider's stored key. Used by the settings UI's "Remove"
    button when the admin wants to disconnect."""
    await _require_admin(request)
    spec = _KEY_PROVIDERS.get(provider)
    if not spec:
        raise HTTPException(status_code=400, detail="unknown provider")
    try:
        await db.set_setting(spec["env"], "")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"persist failed: {exc!r}")
    import os as _os
    _os.environ.pop(spec["env"], None)
    if provider == "gemini":
        _os.environ.pop("GOOGLE_API_KEY", None)
    return {"ok": True}


@app.post("/api/admin/rank-now")
async def admin_rank_now(request: Request):
    """Run the ranking department once, right now: re-score importance,
    cluster same-story-across-outlets, collapse duplicates. Same pass the
    background worker runs every 20 min — this is the manual button.
    Admin-only. Returns the pass stats."""
    await _require_admin(request)
    stats = await asyncio.to_thread(ranker.rank_active)
    cache._store.pop("brief:response", None)
    return {"ok": True, **stats}


@app.get("/api/usage")
async def api_usage(request: Request):
    """LLM cost telemetry + masked key fingerprints. Admin-only —
    surfaces ${} cost per provider plus which key each provider is
    currently using (first 4 + last 4 chars only)."""
    await _require_admin(request)
    import os as _os
    payload = await db.api_usage()
    payload["keys"] = {
        "gemini": {
            "set": bool(_os.getenv("GEMINI_API_KEY") or _os.getenv("GOOGLE_API_KEY")),
            "fingerprint": _mask_api_key(
                _os.getenv("GEMINI_API_KEY") or _os.getenv("GOOGLE_API_KEY")
            ),
            "env_var": (
                "GEMINI_API_KEY" if _os.getenv("GEMINI_API_KEY")
                else "GOOGLE_API_KEY" if _os.getenv("GOOGLE_API_KEY")
                else None
            ),
        },
        "anthropic": {
            "set": bool(_os.getenv("ANTHROPIC_API_KEY")),
            "fingerprint": _mask_api_key(_os.getenv("ANTHROPIC_API_KEY")),
            "env_var": "ANTHROPIC_API_KEY" if _os.getenv("ANTHROPIC_API_KEY") else None,
        },
    }
    return payload


@app.get("/api/ingest/status")
async def ingest_status(request: Request):
    """Live progress for the lab INGESTION MONITOR card. Cheap query —
    safe to poll every few seconds. Admin-only."""
    await _require_admin(request)
    stats = await db.validation_stats()
    total = stats["pending"] + stats["validated"] + stats["failed"]
    done = stats["validated"] + stats["failed"]
    pct = (done / total * 100) if total else 0
    return {
        **stats,
        "total": total,
        "done": done,
        "percent": round(pct, 1),
    }


@app.get("/api/storage")
async def api_storage(request: Request):
    """Disk + memory snapshot for the lab dashboard. Uses only stdlib
    so this works on the minimal Lightsail Python. Admin-only."""
    await _require_admin(request)
    import os as _os
    import platform as _platform
    import resource as _resource
    import socket as _socket
    db_path = db.DB_PATH
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    # Disk space (Unix). Falls back to 0/0 on weird filesystems.
    try:
        st = _os.statvfs(str(db_path.parent))
        disk_free = st.f_bavail * st.f_frsize
        disk_total = st.f_blocks * st.f_frsize
    except Exception:
        disk_free = disk_total = 0
    # Resident memory of this process (Linux returns KB, macOS returns
    # bytes — normalise to bytes).
    try:
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        rss_kb = usage.ru_maxrss
        # macOS reports in bytes; Linux in kilobytes. Heuristic: any
        # value over 4 GB is bytes.
        rss_bytes = rss_kb if rss_kb > 4 * 1024 * 1024 else rss_kb * 1024
    except Exception:
        rss_bytes = 0
    # Mountpoint walk — climb up from the DB file until os.path.ismount
    # returns true. Tells the operator whether the DB sits on the root
    # volume or a separate data mount (matters on Lightsail).
    mountpoint = "/"
    try:
        p = db_path.parent.resolve()
        for _ in range(20):
            if _os.path.ismount(str(p)):
                mountpoint = str(p)
                break
            if str(p) in ("/", "//"):
                break
            p = p.parent
    except Exception:
        pass
    # Filesystem type — only available on Linux via /proc/mounts. macOS
    # falls back to a "n/a" string so the lab UI just hides the row.
    fs_type = "n/a"
    try:
        with open("/proc/mounts", "r") as f:
            best_match_len = -1
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and mountpoint.startswith(parts[1]):
                    if len(parts[1]) > best_match_len:
                        best_match_len = len(parts[1])
                        fs_type = parts[2]
    except Exception:
        pass
    # Cache footprint (number of keys + rough byte size).
    cache_keys = len(cache._store)
    last_prune = await db.get_setting("last_prune", None)
    return {
        "db_bytes": db_bytes,
        "db_path": str(db_path),
        "data_dir": str(db_path.parent),
        "mountpoint": mountpoint,
        "fs_type": fs_type,
        "hostname": _socket.gethostname(),
        "platform": _platform.platform(),
        "pid": _os.getpid(),
        "disk_free": disk_free,
        "disk_total": disk_total,
        "disk_used_pct": (
            round((disk_total - disk_free) / disk_total * 100, 1)
            if disk_total else 0
        ),
        "rss_bytes": rss_bytes,
        "cache_keys": cache_keys,
        "last_prune": last_prune,
    }


@app.post("/api/storage/prune")
async def storage_prune_now(request: Request):
    """Manual prune trigger — useful from the lab when the user
    wants to reclaim space without waiting for the daily cycle.
    Admin-only."""
    await _require_admin(request)
    result = await db.prune_once()
    await db.set_setting("last_prune", result)
    return result


# ── Auth endpoints + admin gate ───────────────────────────────────
# Default credentials: admin / admin (seeded on first boot). Change
# the password right after first login via POST /api/auth/change-password.
async def _require_admin(request: Request) -> dict:
    """Raise 401 if the request isn't from a logged-in admin. Used by
    every /api/lab/* route + the /lab HTML page itself."""
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return user


@app.post("/api/auth/login")
async def auth_login(body: dict, response: Response):
    """Username + password → sets the db_session cookie. Returns the
    user payload (sans hash) so the front-end can flip into admin UI
    right away without a follow-up /whoami round-trip."""
    username = (body or {}).get("username") or ""
    password = (body or {}).get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="username + password required")
    record = await asyncio.to_thread(auth._get_user_by_username_sync, username)
    if not record:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not auth._verify_password(password, record["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = await auth.create_session(record["id"])
    auth.set_session_cookie(response, token)
    return {
        "ok": True,
        "user": {
            "username": record["username"],
            "is_admin": bool(record["is_admin"]),
            "subscription": bool(record["subscription"]),
        },
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        await auth.delete_session(token)
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/whoami")
async def auth_whoami(request: Request):
    user = await auth.current_user(request)
    if not user:
        return {"user": None}
    return {
        "user": {
            "username": user["username"],
            "is_admin": bool(user.get("is_admin")),
            "subscription": bool(user.get("subscription")),
        },
    }


@app.get("/api/auth/profile")
async def auth_profile(request: Request):
    """Full profile data for the /account page — adds created_at on top
    of /whoami so the page can show 'member since'."""
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    rec = await asyncio.to_thread(auth._get_user_by_username_sync, user["username"])
    if not rec:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "username": rec["username"],
        "is_admin": bool(rec["is_admin"]),
        "subscription": bool(rec["subscription"]),
        "created_at": rec["created_at"],
    }


@app.post("/api/auth/signup")
async def auth_signup(body: dict, response: Response):
    """Open signup. New accounts default to non-admin + no subscription;
    only admin/admin (seeded on first boot) can elevate others via direct
    DB edits. Auto-login on success so the user lands signed in."""
    username = ((body or {}).get("username") or "").strip()
    password = (body or {}).get("password") or ""
    if len(username) < 3 or len(username) > 32:
        raise HTTPException(status_code=400, detail="username must be 3-32 chars")
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="username: letters, digits, . _ - only")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="password must be at least 4 chars")
    existing = await asyncio.to_thread(auth._get_user_by_username_sync, username)
    if existing:
        raise HTTPException(status_code=409, detail="username already taken")
    user_id = await auth.create_user(
        username, password, subscription=False, is_admin=False,
    )
    if not user_id:
        raise HTTPException(status_code=500, detail="signup failed")
    # Auto-login: drop a session cookie so the new user lands signed in.
    token = await auth.create_session(user_id)
    auth.set_session_cookie(response, token)
    return {
        "ok": True,
        "user": {"username": username, "is_admin": False, "subscription": False},
    }


@app.post("/api/auth/change-password")
async def auth_change_password(body: dict, request: Request):
    """Logged-in user changes their own password. Requires the old
    password as a confirmation step (so a stolen cookie can't lock
    the real owner out)."""
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    old_pw = (body or {}).get("old_password") or ""
    new_pw = (body or {}).get("new_password") or ""
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="new password too short")
    rec = await asyncio.to_thread(auth._get_user_by_username_sync, user["username"])
    if not rec or not auth._verify_password(old_pw, rec["password_hash"]):
        raise HTTPException(status_code=401, detail="old password incorrect")
    new_hash = auth._hash_password(new_pw)
    def _go():
        from backend.db import _conn as _c
        from contextlib import closing as _cl
        with _cl(_c()) as c:
            c.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, rec["id"]),
            )
    await asyncio.to_thread(_go)
    return {"ok": True}


@app.get("/api/health")
async def health():
    """Tiny liveness probe — no DB hit, no fetches. Caddy / cron /
    uptime checks can poke this without triggering the expensive
    /api/brief path."""
    return {"ok": True}


@app.get("/api/health/extraction")
async def health_extraction(hours: int = 24):
    """Per-outlet reader success rate.

    Public on purpose: this is the view that answers "why won't articles
    open", and it holds no user data. Degrading outlets sort to the top
    so the problem is the first thing on screen.
    """
    hours = min(max(hours, 1), 24 * 30)
    data = await db.extract_health(hours)
    outlets = data.get("outlets") or []

    # A subscription-only outlet failing is not a fault — NYT, FT,
    # Reuters and MarketWatch will paywall us forever, and counting them
    # as "degraded" would leave the page permanently red and therefore
    # ignorable. Split them out: `subscription` is expected behaviour,
    # `degraded` is something that might actually be fixable.
    struggling = [
        o for o in outlets
        if o["attempts"] >= 3 and o["success_rate"] < 0.5
    ]
    subscription = [o for o in struggling if o.get("reason") == "paywall"]
    degraded = [o for o in struggling if o.get("reason") != "paywall"]
    for group in (subscription, degraded):
        group.sort(key=lambda o: (o["success_rate"], -o["attempts"]))

    data["subscription"] = subscription
    data["degraded"] = degraded
    data["status"] = (
        "ok" if not degraded
        else "degraded" if len(degraded) < 4
        else "failing"
    )
    return data


@app.get("/api/health/feeds")
async def health_feeds(request: Request):
    """Probe every configured RSS feed and report which ones are dead.

    Admin-only and not cached: it fires ~130 outbound requests, so it is
    a deliberate diagnostic, not something to poll. A feed that 404s or
    403s silently starves a whole category, and until now that only
    showed up as a line in journalctl.
    """
    await _require_admin(request)
    outlets = getattr(config, "OUTLETS", [])

    sem = asyncio.Semaphore(12)

    async def probe(o: dict) -> dict:
        url = o.get("url") or ""
        name = o.get("name") or "?"
        if not url:
            return {"outlet": name, "ok": False, "status": None, "detail": "no url"}
        async with sem:
            try:
                async with httpx.AsyncClient(
                    headers=reader.BROWSER_HEADERS, follow_redirects=True,
                ) as client:
                    r = await client.get(url, timeout=15.0)
                entries = r.text.count("<item") + r.text.count("<entry")
                return {
                    "outlet": name,
                    "category": o.get("category"),
                    "ok": r.status_code == 200 and entries > 0,
                    "status": r.status_code,
                    "entries": entries,
                    "detail": "" if r.status_code == 200 else f"HTTP {r.status_code}",
                }
            except Exception as exc:
                return {
                    "outlet": name, "category": o.get("category"),
                    "ok": False, "status": None, "entries": 0,
                    "detail": type(exc).__name__,
                }

    results = await asyncio.gather(*[probe(o) for o in outlets])
    failing = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "ok": len(results) - len(failing),
        "failing": sorted(failing, key=lambda r: r["outlet"]),
    }


@app.post("/api/admin/rebuild")
async def admin_rebuild(request: Request, purge_now: int = 0):
    await _require_admin(request)
    """One-shot admin maintenance:
      1. Drop reader_results entries whose stored title is generic
         boilerplate (Editor's choice, Latest News, etc.) — forces a
         fresh trafilatura pass that has a chance to pick the real
         headline via the RSS-title fallback.
      2. Reset every article back to validated=0 so the validation
         worker re-runs the newest heuristics across the full archive.
      3. If `purge_now=1`, immediately delete any article + reader row
         already marked validated=-1. (You can also wait for the worker
         to chew through the queue and call this endpoint again later.)
    Returns counts so the caller can confirm the work landed."""
    needles = [
        "editor's choice", "editor’s choice", "editors choice",
        "editor's pick", "editor’s pick", "editors pick",
        "latest news", "breaking news", "top stories",
        "most read", "most popular", "trending",
        "page not found", "404",
        "편집자 추천", "많이 본 뉴스", "주요 뉴스", "최신 뉴스", "인기 기사",
    ]
    cleared_readers = await db.purge_bad_title_readers(needles)
    reset_rows = await db.reset_validation()
    # Also drop the in-memory caches so the next /api/brief regenerates.
    cache.clear()
    purged = 0
    if int(purge_now or 0):
        purged = await db.purge_failed_articles()
    return {
        "cleared_bad_title_readers": cleared_readers,
        "reset_to_pending": reset_rows,
        "purged_failed_articles": purged,
    }


@app.post("/api/article/delete")
async def delete_article_by_user(body: dict, request: Request):
    """Admin-only article removal. Body: {url, reason, note?}.

    The article row + cached reader body are deleted permanently, and
    the URL is added to `blocked_urls` so the RSS fetcher can never
    bring the same story back via a future poll. `reason` is one of
    the short slugs from the front-end picker; `note` is optional
    free-text. Gated to admin because deletion is destructive and
    can't be undone — anonymous visitors browsing the feed can't take
    articles off it for everyone else.
    """
    await _require_admin(request)
    url = (body or {}).get("url") or ""
    reason = (body or {}).get("reason") or "other"
    note = (body or {}).get("note") or ""
    if not url.startswith(("http://", "https://")):
        return {"error": "invalid url"}
    # Current picker slugs (in display order) + legacy aliases from
    # older clients still in the wild. Anything else falls to "other".
    valid_reasons = {
        "too-short", "irrelevant", "quality", "clickbait",
        "duplicate", "broken", "other",
        # legacy
        "incomplete", "misleading",
    }
    if reason not in valid_reasons:
        reason = "other"
    result = await db.delete_article_by_user(url, reason, note or None)
    # Drop in-memory caches so the next /api/brief reflects the removal
    # without waiting for the 2-minute TTL.
    cache.clear()
    await db.bump_counter(f"user_delete_{reason}")
    return {"ok": True, **result}


@app.post("/api/admin/archive-old")
async def admin_archive_old(request: Request, days: int = 7):
    """Move articles older than `days` from active → archived. Archived
    articles aren't deleted, just hidden from /api/brief + /api/page by
    default. They stay readable via lab tools and the reader_results
    cache. Admin-only."""
    await _require_admin(request)
    days = max(1, min(int(days or 7), 365))
    moved = await db.archive_old_articles(days_old=days)
    cache.clear()
    stats = await db.archive_stats()
    return {"archived_now": moved, "days": days, **stats}


@app.post("/api/admin/unarchive-all")
async def admin_unarchive_all(request: Request):
    """Pull every archived article back into the active pool. Used
    when the operator wants to surface the long tail or has misjudged
    a previous archive run. Admin-only."""
    await _require_admin(request)
    moved = await db.unarchive_all()
    cache.clear()
    stats = await db.archive_stats()
    return {"unarchived": moved, **stats}


@app.get("/api/lab/archive-stats")
async def lab_archive_stats(request: Request):
    """Active vs archived counts for the lab dashboard. Admin-only."""
    await _require_admin(request)
    return await db.archive_stats()


@app.post("/api/admin/factory-reset")
async def admin_factory_reset(request: Request):
    """Wipe articles + reader_results + blocked_urls back to empty +
    VACUUM. Keeps users/sessions so the admin stays logged in. The
    next /api/brief tick will reseed from live RSS feeds. Admin-only,
    irreversible."""
    await _require_admin(request)
    result = await db.factory_reset()
    cache.clear()
    return {"ok": True, **result}


@app.post("/api/admin/run-validation")
async def admin_run_validation(request: Request, batch_size: int = 20):
    """Process up to `batch_size` pending articles inline (HTTP fetch +
    validator). Manual replacement for the disabled validation worker.
    Returns counts so the operator can chip through the backlog
    deliberately instead of running a worker that grinds the box."""
    await _require_admin(request)
    batch = await db.list_unvalidated(limit=max(1, min(batch_size, 50)))
    if not batch:
        return {"examined": 0, "passed": 0, "failed": 0}
    results = await asyncio.gather(
        *[_validate_one(a) for a in batch], return_exceptions=True,
    )
    passed = failed = 0
    for art, res in zip(batch, results):
        url = art.get("url")
        if not url:
            continue
        if isinstance(res, Exception):
            status, reason = -1, f"crash:{type(res).__name__}"
        else:
            status, reason = res
        try:
            await db.set_article_validated(url, status, reason)
            if status == 1: passed += 1
            else:           failed += 1
        except Exception:
            pass
    return {"examined": len(batch), "passed": passed, "failed": failed}


@app.post("/api/admin/purge-failed")
async def admin_purge_failed(request: Request):
    """Drop every article currently marked validated=-1 along with
    its cached reader body. Call this after the validation worker has
    re-chewed the archive — anything still failing then is permanently
    unrecoverable and should not stay in the DB. Admin-only."""
    await _require_admin(request)
    purged = await db.purge_failed_articles()
    cache.clear()
    return {"purged": purged}


_STRICT_RUNNING = False


@app.post("/api/admin/strict-revalidate")
async def admin_strict_revalidate(request: Request, batch_size: int = 50):
    """Resets every article to validated=0 and kicks off a fast async
    revalidation in the background. Returns immediately — poll
    /api/ingest/status to watch progress; when pending hits 0, the
    background task auto-purges everything that failed and clears the
    in-memory caches so /api/brief picks up the new feed.

    Use after tightening validator thresholds. Synchronous variants
    would block past Caddy/uvicorn timeouts on archives this size.
    """
    await _require_admin(request)
    global _STRICT_RUNNING
    if _STRICT_RUNNING:
        return {"ok": False, "message": "A strict revalidation is already in progress."}

    reset = await db.reset_validation()
    cache.clear()
    batch_size = max(1, min(batch_size, 100))

    async def _strict_runner():
        global _STRICT_RUNNING
        _STRICT_RUNNING = True
        examined = passed = failed = 0
        try:
            while True:
                batch = await db.list_unvalidated(limit=batch_size)
                if not batch:
                    break
                results = await asyncio.gather(
                    *[_validate_one(a) for a in batch], return_exceptions=True,
                )
                for art, res in zip(batch, results):
                    url = art.get("url")
                    if not url:
                        continue
                    if isinstance(res, Exception):
                        status, reason = -1, f"crash:{type(res).__name__}"
                    else:
                        status, reason = res
                    try:
                        await db.set_article_validated(url, status, reason)
                        examined += 1
                        if status == 1:
                            passed += 1
                            await db.bump_counter("ingest_pass")
                        else:
                            failed += 1
                            await db.bump_counter("ingest_fail")
                    except Exception as exc:
                        print(f"[strict] mark failed for {url[:60]}: {exc!r}", flush=True)
                # Yield so other requests stay responsive.
                await asyncio.sleep(0.05)
            try:
                purged = await db.purge_failed_articles()
                cache.clear()
                print(
                    f"[strict] done. examined={examined} passed={passed} "
                    f"failed={failed} purged={purged}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[strict] purge failed: {exc!r}", flush=True)
        finally:
            _STRICT_RUNNING = False

    asyncio.create_task(_strict_runner())
    return {
        "ok": True,
        "reset": reset,
        "message": "Strict revalidation started. Watch /api/ingest/status — when pending hits 0 the failed rows auto-purge.",
    }


# In-flight guard for the brief rebuild + a timestamp for stale-while-
# revalidate. The user reported the phone showing the page chrome
# instantly but the article wall hanging — that was cache-miss callers
# being asked to do the full 5-15 s rebuild themselves. Now:
#   - fresh cache (< BRIEF_CACHE_TTL old) → served instantly
#   - stale cache (within BRIEF_STALE_GRACE) → served instantly + a
#     background rebuild kicks off so the NEXT request gets fresh data
#   - no cache at all → tries a fast DB-only response; if even that's
#     empty, does the full rebuild but with a hard 12 s timeout so the
#     user is never stuck on a spinner for more than a few seconds
BRIEF_CACHE_TTL = 600          # 10 min — articles change slowly
BRIEF_STALE_GRACE = 6 * 3600   # 6h: serve stale for 6 hours if rebuild fails
_brief_rebuilding = False


# ── Brief response scrubbers ────────────────────────────────────────
# Some legacy DB rows still carry raw RSS <description> markup in the
# summary column (Hankyoreh's image-thumbnail <table>, etc.) — those
# predate the rss._clean_summary scrub. We re-strip at the API boundary
# so even old rows render clean. Also enforces a final global per-outlet
# ceiling so the feed user never sees the same newspaper five times in
# a row, regardless of how it got past the earlier passes.
_API_HTML_TAG_RE = re.compile(r"<[^>]+>")
_API_WS_RE = re.compile(r"\s+")
_FEED_FIELDS_TO_SCRUB = ("title", "dek", "summary", "title_ko", "dek_ko")
# Maximum copies of the same outlet allowed in the final mixed list.
# Belt-and-suspenders next to sorter.GLOBAL_PER_OUTLET_CAP — the sorter
# only runs in _brief_build_full; the fast path (_brief_db_fallback)
# bypasses it, so this enforces the cap there too.
_API_GLOBAL_PER_OUTLET_CAP = 3


def _scrub_text(s: str | None) -> str:
    if not s:
        return ""
    out = _API_HTML_TAG_RE.sub(" ", s)
    out = html.unescape(out)
    out = _API_HTML_TAG_RE.sub(" ", out)
    return _API_WS_RE.sub(" ", out).strip()


def _polish_mixed(payload: dict) -> dict:
    """In-place: strip HTML out of every text field on every mixed item,
    then drop duplicate-outlet pile-ups past _API_GLOBAL_PER_OUTLET_CAP.
    Idempotent — safe to call on a cached payload."""
    mixed = payload.get("mixed") or []
    if not isinstance(mixed, list):
        return payload
    per_outlet: dict[str, int] = {}
    out_list: list[dict] = []
    for it in mixed:
        if not isinstance(it, dict):
            continue
        for k in _FEED_FIELDS_TO_SCRUB:
            v = it.get(k)
            if isinstance(v, str) and ("<" in v or "&" in v):
                it[k] = _scrub_text(v)
        outlet = (it.get("outlet") or "").strip()
        if outlet:
            n = per_outlet.get(outlet, 0)
            if n >= _API_GLOBAL_PER_OUTLET_CAP:
                continue
            per_outlet[outlet] = n + 1
        out_list.append(it)
    if len(out_list) != len(mixed):
        payload["mixed"] = out_list
        payload["total_mixed"] = len(out_list)
    return payload


def _brief_db_fallback() -> dict | None:
    """Synchronously build a tiny brief payload from SQLite only — no
    RSS fetch, no LLM headline, no image scraping. Used as the first
    response when the in-memory cache is cold on a fresh user.

    Round-robin by category: take rank-1 from each chip in order, then
    rank-2 from each, etc. So the feed reads as
        WLD-1, MKT-1, BIZ-1, TCH-1, AI-1, CRYPT-1, SCI-1, GEO-1,
        OP-ED-1, 한국-1, K-ENT-1, WLD-2, MKT-2, ...
    This matches the chip nav order exactly. No single category can
    dominate the top of the wall even when one has a huge backlog.
    SQLite supports ROW_NUMBER + CTE since 3.25; Ubuntu 22.04 has 3.37+.
    """
    try:
        import sqlite3
        from contextlib import closing as _cl
        with _cl(db._conn()) as c:
            rows = c.execute(
                """
                WITH ranked AS (
                  SELECT
                    url, title, image, outlet, category, lang, summary,
                    score, title_ko, dek_ko, translated_at, published_at,
                    premium, weight, fetched_at,
                    ROW_NUMBER() OVER (
                      PARTITION BY category
                      -- Importance first, then recency as the tiebreak.
                      -- The ranker persists premium/score (authority +
                      -- cross-outlet corroboration), so the biggest
                      -- stories lead each desk and DON'T reshuffle every
                      -- ingest — only when their importance actually
                      -- changes. published_at (ISO-8601) breaks ties.
                      ORDER BY premium DESC, score DESC, published_at DESC
                    ) AS cat_rank,
                    CASE category
                      WHEN 'world'   THEN 1
                      WHEN 'econ'    THEN 2
                      WHEN 'biz'     THEN 3
                      WHEN 'tech'    THEN 4
                      WHEN 'ai'      THEN 5
                      WHEN 'crypto'  THEN 6
                      WHEN 'science' THEN 7
                      WHEN 'geo'     THEN 8
                      WHEN 'opinion' THEN 9
                      WHEN 'korea'   THEN 10
                      WHEN 'kent'    THEN 11
                      ELSE 99
                    END AS cat_order
                  FROM articles
                  -- Prefer clean, body-backed articles but NEVER starve
                  -- to a blank feed. The old strict gate required
                  -- validated=1 OR a persisted reader body; once the
                  -- archive worker drained every validated row out of the
                  -- active pool (and validation wasn't running to refill
                  -- it), that gate matched zero rows and /api/brief served
                  -- an empty "Loading feed…" payload for weeks. Relaxed
                  -- 2026-07-05 to just active + non-failed, which matches
                  -- what /api/page already shows on the category tabs.
                  WHERE archived = 0 AND validated != -1
                    AND (dup_of IS NULL OR dup_of = '')
                )
                SELECT url, title, image, outlet, category, lang, summary,
                       score, title_ko, dek_ko, translated_at, published_at,
                       premium, weight
                FROM ranked
                WHERE cat_rank <= 10
                ORDER BY cat_rank ASC, cat_order ASC
                LIMIT 120
                """
            ).fetchall()
            if not rows:
                return None
            mixed = []
            for r in rows:
                mixed.append({
                    "kind": "news",
                    "url": r["url"],
                    "title": r["title"] or "",
                    "image": r["image"],
                    "outlet": r["outlet"],
                    "category": r["category"],
                    "lang": r["lang"] or "en",
                    "dek": r["summary"] or "",
                    "ts": r["published_at"],
                    "score": r["score"] or 0,
                    "premium": bool(r["premium"]),
                    "weight": float(r["weight"] or 1.0),
                    "title_ko": r["title_ko"],
                    "dek_ko": r["dek_ko"],
                    "translated_at": r["translated_at"],
                    "tickers": [], "sparks": {},
                })
        return {
            "profile": {
                "bio": config.PROFILE.bio,
                "keywords": config.PROFILE.keywords,
                "primary_lang": getattr(config.PROFILE, "primary_lang", "en"),
            },
            "tape": [], "headline": "",
            "mixed": mixed,
            "cat_counts": {},
            "total_mixed": len(mixed),
            "outlets_count": 0,
            "whales": [], "trades": [], "youtube": [],
            "db_total_articles": min(130, len(mixed) * 6),
            "_fast": True,
        }
    except Exception as exc:
        print(f"[brief-fast] DB fallback failed: {exc!r}", flush=True)
        return None


async def _kickoff_brief_rebuild():
    """Run the FULL brief assembly in the background and write the
    result into both cache buckets. Bounded by a 60 s hard timeout so
    a stuck RSS fetch or hung LLM call can't pin the rebuilding flag
    forever (which would mean no future caller could ever trigger
    another rebuild)."""
    global _brief_rebuilding
    if _brief_rebuilding:
        return
    _brief_rebuilding = True
    try:
        # Body-first ops mode: rebuild from the DB fast-path, NOT from the
        # full RSS→curator→LLM chain. The full chain pulls fresh articles
        # that are validated=0 with no body, so its post-gate result is
        # almost empty (~10 items). The fast-path queries the DB for rows
        # that already have validated=1 OR a stored body — i.e. the only
        # articles users can actually open. _polish_mixed strips HTML and
        # caps per outlet. This is the same data path /api/brief uses on
        # cache miss, so the cache and the cache-miss response stay
        # consistent.
        fast = _brief_db_fallback()
        if fast is None:
            print("[brief-bg-refresh] fast-path returned None, skipping", flush=True)
            return
        payload = _polish_mixed(fast)
        cache.set("brief:response", payload, BRIEF_CACHE_TTL)
        cache.set("brief:response_stale", payload, BRIEF_STALE_GRACE)
        news = sum(1 for m in (payload.get("mixed") or []) if m.get("kind") == "news")
        print(f"[brief-bg-refresh] cache repopulated (news={news})", flush=True)
    except Exception as exc:
        print(f"[brief-bg-refresh] failed: {exc!r}", flush=True)
    finally:
        _brief_rebuilding = False


def _empty_brief_payload(msg: str = "Loading…") -> dict:
    """Last-resort payload when everything else has failed or timed out.
    Frontend still renders the chrome; status line shows the message."""
    return {
        "profile": {
            "bio": getattr(config.PROFILE, "bio", ""),
            "keywords": getattr(config.PROFILE, "keywords", []),
            "primary_lang": getattr(config.PROFILE, "primary_lang", "en"),
        },
        "tape": [], "headline": msg,
        "mixed": [], "cat_counts": {}, "total_mixed": 0,
        "outlets_count": 0, "whales": [], "trades": [], "youtube": [],
        "db_total_articles": 0, "_loading": True,
    }


@app.get("/api/brief")
async def brief():
    """The main payload: ticker tape + mixed feed + sidebars + summary.

    Cache-first with stale-while-revalidate AND a hard 6 s timeout on
    every code path so phone users on slow connections / Caddy with a
    30 s upstream cap never sit on a hanging request.
    """
    try:
        # Fresh cache → ship it. Polish first so legacy cached payloads
        # (built before the HTML scrubber) get cleaned on the way out.
        cached = cache.get("brief:response")
        if cached is not None:
            return _polish_mixed(dict(cached))

        # No fresh cache. Try the stale grace bucket.
        # NOT triggering a rebuild here either — see comment below.
        stale = cache.get("brief:response_stale")
        if stale is not None:
            out = dict(stale)
            out["_stale"] = True
            return _polish_mixed(out)

        # No cache at all. Serve the cheap DB-only payload — and
        # DO NOT kick off a background rebuild here. On a 512MB
        # Lightsail box the full rebuild's RSS+image-scrape+LLM chain
        # was OOM-killing uvicorn under traffic. Rebuilds happen on a
        # slow scheduled cadence below (see _brief_refresh_worker).
        try:
            fast = _brief_db_fallback()
            if fast is not None:
                return _polish_mixed(fast)
        except Exception as exc:
            print(f"[brief] fast-path crashed: {exc!r}", flush=True)

        # No cache AND no DB rows. Block once with a hard timeout — if
        # the full build runs long the user gets the empty-loading
        # payload + a background rebuild that fills cache for the next
        # request. Better than 502.
        try:
            payload = await asyncio.wait_for(_brief_build_full(), timeout=10.0)
            payload = _polish_mixed(payload)
            cache.set("brief:response", payload, BRIEF_CACHE_TTL)
            cache.set("brief:response_stale", payload, BRIEF_STALE_GRACE)
            return payload
        except asyncio.TimeoutError:
            asyncio.create_task(_kickoff_brief_rebuild())
            return _empty_brief_payload("Loading feed…")
    except Exception as exc:
        # Absolute last-resort: log + ship the empty payload so the
        # endpoint NEVER returns a 5xx. Caddy proxies a 5xx to the user
        # as 502, which is what they reported.
        print(f"[brief] handler crashed, serving empty: {exc!r}", flush=True)
        return _empty_brief_payload("Loading…")


async def _brief_build_full() -> dict:
    """The actual full brief assembly: RSS fetch → curator → sorter →
    LLM headline → DB persistence. Extracted from the @app.get handler
    so background rebuilds can call it without going through the
    cache/fast-path gate."""

    articles, whale_moves_raw, insider_trades_raw, yt_raw, tape = await asyncio.gather(
        rss.fetch_all(),
        whales.fetch(),
        politicians.fetch(),
        youtube.fetch(),
        prices.fetch_tape(),
    )

    # Drop anything the user has explicitly removed via the ×-on-card
    # flow. blocked_urls is consulted once per /api/brief — cheap query
    # (PK lookup, table is tiny) and ensures the deletion is visible
    # right away even if the in-memory `rss:all` cache still has the URL.
    try:
        blocked = await db.blocked_url_set()
        if blocked:
            articles = [a for a in articles if a.url not in blocked]
    except Exception as exc:
        print(f"[brief] blocked-url lookup failed: {exc!r}", flush=True)

    # ── Stage A: persist EVERY fetched article first. ───────────────
    # Before this fix the curator picked top 250 and only those got
    # saved to the DB — which meant a lot of outlets fetched articles
    # but the lab "Quality Inspector" still showed NO ARTICLES because
    # the article never made it past the in-memory curator. Now the
    # DB holds the full archive of what each outlet actually delivered.
    try:
        baseline_rows = [
            {
                "url": a.url,
                "title": a.title,
                "image": a.image,
                "outlet": a.outlet,
                "category": a.category,
                "lang": a.lang,
                "summary": a.summary,
                "published_at": a.published,
            }
            for a in articles if a.url
        ]
        if baseline_rows:
            await db.upsert_articles(baseline_rows)
    except Exception as exc:
        print(f"[db] baseline upsert failed: {exc!r}", flush=True)

    # Over-fetch so we still hit TOP_K visible items after enrich_top
    # throws out paywalls / logos.
    raw_top = await curator.rank(articles, top_k=int(config.TOP_K * 1.7))
    # Probe + filter only this ranked head — way cheaper than touching
    # every RSS candidate.
    top = await rss.enrich_top(raw_top, top_n=int(config.TOP_K * 1.5))
    # Dedup pass: collapse same-story-from-multiple-outlets clusters
    # down to at most 2 picks each (best by quality), so the per-
    # category quota doesn't get filled with Reuters/AP/Bloomberg
    # copies of the same headline.
    top, dedup_stats = dedup.deduplicate(top)
    if dedup_stats["removed"]:
        print(
            f"[dedup] {dedup_stats['clusters']} cluster(s), "
            f"removed {dedup_stats['removed']}, "
            f"kept {len(top)}",
            flush=True,
        )
    # Smart sorter: enforce per-category quotas, per-outlet caps, premium
    # weight, and top-up. Replaces the naive `top[:TOP_K]` slice with a
    # balanced page that the curator could never produce alone.
    top = sorter.balance(top, target_total=config.TOP_K)

    # Body-first quality gate. The user-facing rule: every article on the
    # main page must already have its body persisted to reader_results.
    # Drop an item unless:
    #   - validated=1  (worker confirmed extraction + validator passed), OR
    #   - validated=0  AND a real body exists in reader_results (a prior
    #                  worker pass / user click warmed it).
    # validated=-1 is always dropped. This keeps the feed clean of "could
    # not extract the article body" surprises and is the body-first
    # invariant the spec asks for.
    try:
        top_urls = [t.get("url") for t in top if t.get("url")]
        states = await db.get_validation_states(top_urls)
        body_present = await db.reader_urls_present(top_urls)

        def _keep(item):
            url = item.get("url")
            if not url:
                return False
            v = states.get(url, 0)
            if v == -1:
                return False
            if v == 1:
                return True
            return url in body_present

        before = len(top)
        top = [t for t in top if _keep(t)]
        dropped = before - len(top)
        if dropped:
            print(f"[gate] dropped {dropped} items (no validated=1 / no body)", flush=True)
    except Exception as exc:
        print(f"[gate] validation lookup failed: {exc!r}", flush=True)

    await tickers.enrich_with_sparks(top)

    # Annotate each top item with its saved card-level translation
    # (if any). This is what powers the neon-border + ✦한 badge on
    # the front end — cards with a stored Korean headline show the
    # translation toggle even before the user opens them.
    try:
        urls = [t.get("url") for t in top if t.get("url")]
        trans_map = await db.get_card_translations(urls)
        for t in top:
            tr = trans_map.get(t.get("url"))
            if tr:
                t["title_ko"] = tr["title_ko"]
                t["dek_ko"] = tr["dek_ko"]
                t["translated_at"] = tr["translated_at"]
    except Exception as exc:
        print(f"[db] translation lookup failed: {exc!r}", flush=True)

    whale_moves = [w.to_dict() for w in whale_moves_raw]
    insider_trades = [t.to_dict() for t in insider_trades_raw]
    yt = [y.to_dict() for y in yt_raw]

    mixed = mixer.merge(
        mixer.from_news(top),
        mixer.from_whales(whale_moves),
        mixer.from_trades(insider_trades),
        mixer.from_videos(yt),
    )

    # Per-category counts for the chip nav — sent as a tiny dict so the
    # frontend can show real totals without us shipping every article.
    cat_counts: dict[str, int] = {}
    premium_count = 0
    for m in mixed:
        if m.get("kind") != "news":
            continue
        c = m.get("category") or "world"
        cat_counts[c] = cat_counts.get(c, 0) + 1
        if m.get("premium"):
            premium_count += 1
    cat_counts["all"] = sum(v for k, v in cat_counts.items())
    cat_counts["premium"] = premium_count

    headline = await summary.generate(mixed)

    # Trim the wire payload aggressively. The site previously shipped 62
    # mixed items + the DB pager exposed 150+ pages worth of older
    # articles — way too much for the user (and Lightsail's 512MB box
    # struggled). Now: top 24 only on /api/brief, pages 2+ come from a
    # DB query that excludes archived rows so the total page count
    # stays in the low double digits.
    INITIAL_WIRE_ITEMS = 24
    mixed_wire = mixed[:INITIAL_WIRE_ITEMS]

    # Hard cap on how deep the front-end pager can go. The user
    # explicitly: "we don't need that many articles to be shown" — so
    # even if the DB has 3000 active rows, only the top 130 (≈ 10
    # pages of 13) are reachable. The archive worker rotates the
    # cap-defining set hourly: the highest-quality, premium-skewed,
    # recent articles bubble to the top, older ones drop off.
    MAX_VISIBLE_ARTICLES = 130

    payload = {
        "profile": {
            "bio": config.PROFILE.bio,
            "keywords": config.PROFILE.keywords,
            "primary_lang": getattr(config.PROFILE, "primary_lang", "en"),
        },
        "tape": [q.to_dict() for q in tape],
        "headline": headline,
        "mixed": mixed_wire,
        "cat_counts": cat_counts,
        "total_mixed": len(mixed),
        # `top` and `by_outlet` were unused on the frontend and just
        # bloated the response — dropped.
        "outlets_count": len({a.outlet for a in articles}),
        "whales": whale_moves,
        "trades": insider_trades,
        "youtube": yt,
    }
    # Cache writes happen in the calling _kickoff_brief_rebuild /
    # brief() handler, NOT here, so a partial assembly never leaves
    # half-baked entries in the bucket. (Keeps _brief_build_full pure.)

    # Persist what we just decided to show. Survives restart and
    # powers the /lab dashboard counts.
    try:
        # Look up premium/weight/quality from the sorter-annotated `top`
        # list (keyed by URL) so the DB row carries those fields too.
        top_by_url = {t.get("url"): t for t in top if t.get("url")}
        rows = []
        for m in mixed:
            if m.get("kind") != "news" or not m.get("url"):
                continue
            src = top_by_url.get(m["url"], {})
            rows.append({
                "url": m.get("url"),
                "title": m.get("title"),
                "image": m.get("image"),
                "image_source": m.get("image_source"),
                "outlet": m.get("outlet"),
                "category": m.get("category"),
                "lang": m.get("lang"),
                "summary": m.get("dek") or "",
                "score": m.get("score") or 0,
                "why": m.get("why"),
                "tier": m.get("tier"),
                "premium": bool(src.get("premium")),
                "weight": float(src.get("weight") or 1.0),
                "quality": float(src.get("quality") or 0),
                "published_at": m.get("ts"),
            })
        await db.upsert_articles(rows)
        await db.bump_counter("articles_ingested", by=len(rows))

        # Tell the frontend how deep the archive goes so its pager can
        # render however many pages the DB actually supports.
        # Capped so the pager never advertises more pages than the
        # hard ceiling above. The DB still has the long tail (archived
        # rows + the rest of the active set); the user just can't page
        # into it from the regular UI.
        payload["db_total_articles"] = min(
            await db.count_articles(), MAX_VISIBLE_ARTICLES,
        )
    except Exception as exc:
        print(f"[db] upsert articles failed: {exc}")

    return payload


@app.post("/api/refresh")
async def refresh(request: Request):
    """Admin-only — drop every in-memory cache key so the next /api/brief
    pulls a fresh RSS sweep + re-runs the curator."""
    await _require_admin(request)
    cache.clear()
    return {"status": "ok"}


def _detect_lang(text: str) -> str:
    """Majority-rules language detect for our two languages.
    Mixed-language titles (e.g. an English headline that ends with
    Korean publisher attribution) used to be flagged 'ko' on the
    first Hangul char and lose their translate button. Count Korean
    vs Latin chars and pick the dominant side. Tie → default English."""
    if not text:
        return "en"
    ko = 0
    en = 0
    for c in text:
        if "가" <= c <= "힣":
            ko += 1
        elif "a" <= c.lower() <= "z":
            en += 1
    if ko == 0 and en == 0:
        return "en"
    return "ko" if ko > en else "en"


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

# "Related links" tail markers — Yonhap, AP, Reuters, AFP and similar
# wire services like to dump a list of other headlines at the END of
# the article body. trafilatura grabs them as paragraphs even though
# they're navigational chrome, not actual story content. Two reliable
# tells:
#   1. Yonhap-style headline tags:  "(LEAD)", "(URGENT)", "(3rd LD)",
#      "(5th LD)", "(2nd UPDATE)"
#   2. Lines ending in a single trailing dash (Yonhap's link separator):
#      "BTS shows in N. America draw 840,000 concertgoers: agency -"
# Once we hit one, EVERYTHING that follows is overwhelmingly more of
# the same — bail out and treat the body as ended.
_HEADLINE_LINK_TAG_RE = re.compile(
    r"^\s*\(\s*(?:LEAD|URGENT|UPDATE|"
    r"\d+(?:st|nd|rd|th)\s*(?:LD|UPDATE)|"
    r"UPDATE\s*\d*)\s*\)",
    re.IGNORECASE,
)
_HEADLINE_LINK_TRAIL_RE = re.compile(r"\s[-–—]\s*$")


def _looks_like_related_link(line: str) -> bool:
    """True for navigational headline links that get appended after
    the real article body."""
    if not line:
        return False
    s = line.strip()
    if _HEADLINE_LINK_TAG_RE.match(s):
        return True
    # Trailing-dash headline ONLY counts as a link when the line is
    # short (typical headline length). Real paragraphs that happen to
    # end with em-dash punctuation are usually 200+ chars.
    if len(s) < 200 and _HEADLINE_LINK_TRAIL_RE.search(s):
        return True
    return False


# Safety cap on what the reader modal shows. User explicitly asked for
# the FULL article body — no mid-article cutoff. These ceilings now sit
# well above any real article and only guard against trafilatura
# pathological output (runaway loops over hundreds of empty <p> tags).
_PARA_CHAR_BUDGET = 60_000   # ≈ 12k words — longer than any feature
_PARA_HARD_MAX = 200         # essentially unbounded for real articles


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


def _dedup_with_title(paras: list[str], title: str) -> list[str]:
    """Drop paragraphs that are just a repeat of the article title +
    dedup case-insensitive. Al Jazeera and a few publishers ship the
    title literally as the first paragraph (sometimes 2-3 times)."""
    seen: set[str] = set()
    out: list[str] = []
    t = (title or "").strip().lower()
    for p in paras:
        p_norm = p.strip().lower()
        if not p_norm:
            continue
        if t and (p_norm == t or (p_norm.startswith(t) and len(p_norm) <= len(t) + 4)):
            continue
        if p_norm in seen:
            continue
        seen.add(p_norm)
        out.append(p)
    return out


def _scrub_paragraph(p: str) -> str:
    """Run the paragraph-scrub steps that _split_paragraphs would have
    applied at fetch time — but on a single string. Used to clean
    cached paragraphs on read, since the reader_results table holds
    pre-existing payloads that pre-date current filter improvements
    (e.g. CoinDesk's `![alt](/_next/image?…)` markdown leaks)."""
    if not p:
        return ""
    p = _IMG_MD_RE.sub("", p)
    p = _scrub_html_artifacts(p)
    p = _EMOJI_RE.sub("", p).strip()
    if _GARBLE_CHAR in p:
        return ""
    if _CREDIT_PREFIX_RE.match(p):
        return ""
    if _CREDIT_WORDS_RE.search(p):
        return ""
    if _SECTION_HEADER_RE.match(p) or _BRACKETED_HEADER_RE.match(p):
        return ""
    if _DECORATIVE_DIVIDER_RE.search(p):
        return ""
    if _ATTR_FRAG_RE.search(p) and len(_ATTR_FRAG_RE.findall(p)) >= 2:
        return ""
    return p


def _scrub_cached_paragraphs(payload: dict) -> dict:
    """Defensive: re-run paragraph filters AND title-repeat dedup on a
    cached reader payload before returning. No-op for fresh paragraphs."""
    paras = payload.get("paragraphs") or []
    cleaned: list[str] = []
    for p in paras:
        s = _scrub_paragraph(p)
        if s and len(s) >= 30:
            cleaned.append(s)
    cleaned = _dedup_with_title(cleaned, payload.get("title") or "")
    if cleaned != paras:
        payload = {**payload, "paragraphs": cleaned}
    return payload


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
        # Related-links tail (Yonhap "(LEAD)…", trailing " -" headlines):
        # once we hit one, every line after is usually another link of
        # the same shape. Bail out of the loop entirely.
        if _looks_like_related_link(p):
            break
        out.append(p)
        total_chars += len(p)
        if len(out) >= _PARA_HARD_MAX or total_chars >= _PARA_CHAR_BUDGET:
            break
    return out


# ── Reader failure handling ──────────────────────────────────────
# Publishers block us for reasons we cannot (and should not try to)
# route around: hard paywalls, bot walls, geo gates. When live
# extraction can't reach the body we still hold the outlet's own RSS
# summary for that story, so the reader shows that plus a prominent
# link to the original instead of a dead end.
_READER_FAIL_COPY = {
    "paywall": (
        "이 기사는 유료 구독자 전용입니다 — 저장된 요약만 표시합니다.",
        "This story is behind the publisher's paywall — showing the saved summary.",
    ),
    "blocked": (
        "매체가 본문 접근을 차단했습니다 — 저장된 요약만 표시합니다.",
        "The publisher blocked automated access — showing the saved summary.",
    ),
    "notfound": (
        "원문이 삭제되었거나 이동했습니다 — 저장된 요약만 표시합니다.",
        "The original article has moved or been removed — showing the saved summary.",
    ),
    "timeout": (
        "매체 응답이 너무 느립니다 — 저장된 요약만 표시합니다.",
        "The publisher's site timed out — showing the saved summary.",
    ),
}
_READER_FAIL_DEFAULT = (
    "본문을 불러오지 못했습니다 — 저장된 요약만 표시합니다.",
    "Couldn't load the full article — showing the saved summary.",
)


def _reader_fallback_from_row(
    url: str,
    article_row: dict | None,
    err: "reader.ExtractError | None" = None,
) -> dict | None:
    """Build a reader payload from the stored articles row when live
    extraction can't reach the publisher. Uses the card summary as the
    body so the modal shows real text + the "open original" link instead
    of a hard error. Returns None only when we have literally nothing
    worth showing (no title)."""
    if not article_row:
        return None
    title = (article_row.get("title") or "").strip()
    if not title:
        return None
    summary = (article_row.get("summary") or "").strip()
    paras = _split_paragraphs(summary) if summary else []
    lang = (article_row.get("lang") or _detect_lang(title) or "en")
    reason = (err.reason if err else "") or "error"
    ko_copy, en_copy = _READER_FAIL_COPY.get(reason, _READER_FAIL_DEFAULT)
    note = ko_copy if lang == "ko" else en_copy
    return {
        "url": url,
        "final_url": (err.final_url if err else None) or url,
        "title": title,
        "image": article_row.get("image"),
        "byline": None,
        "excerpt": summary[:300],
        "lang": lang,
        "word_count": len(summary.split()),
        "paragraphs": paras,
        "partial": True,
        "fail_reason": reason,
        "note": note,
    }


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

    # Look up the article row up-front so the RSS-supplied headline is
    # available as a fallback for any path that lands on a generic
    # trafilatura title like "Editor's choice".
    article_row = await db.get_article(url) if hasattr(db, "get_article") else None
    fallback_title = (article_row or {}).get("title") if article_row else None
    outlet = (article_row or {}).get("outlet") or ""

    # /status answers "did this article open with a real body?", so every
    # return path below logs — including cache hits. Logging only the
    # cache-miss path made the success rate meaningless: successes get
    # cached and stop being counted, while fallbacks keep re-logging.
    async def _served(payload: dict) -> dict:
        await db.record_extract_result(
            outlet, url,
            not payload.get("partial"),
            payload.get("fail_reason") or "",
        )
        return payload

    full_key = f"article_reader:{url}"
    cached = cache.get(full_key)
    if cached is not None:
        await db.bump_counter("reader_cache_hits_mem")
        cached = _scrub_cached_paragraphs(cached)
        if fallback_title and _is_bad_title(cached.get("title") or ""):
            cached = {**cached, "title": fallback_title}
        return await _served(cached)

    # SQLite second-level cache — survives restart. Keeps trafilatura
    # work that's already been paid for.
    saved = await db.get_reader(url)
    if saved is not None:
        # Apply the latest paragraph filters before serving, so stored
        # paragraphs from before a filter improvement get cleaned up on
        # the fly without forcing a re-extract.
        saved = _scrub_cached_paragraphs(saved)
        if fallback_title and _is_bad_title(saved.get("title") or ""):
            saved = {**saved, "title": fallback_title}
        cache.set(full_key, saved, 86400)
        await db.bump_counter("reader_cache_hits_disk")
        return await _served(saved)

    reading, err = await reader.extract_detailed(url)
    if not reading:
        # Live extraction failed (paywall / bot wall / dead URL). Record
        # it against the outlet so /api/health shows which sources are
        # degrading, then fall back to the stored summary rather than
        # handing the user a dead end.
        fallback = _reader_fallback_from_row(url, article_row, err)
        if fallback is not None:
            await db.bump_counter("reader_fallback_summary")
            # Cache the fallback for 30 min. Without this, every click on
            # a paywalled story re-hit the publisher AND logged another
            # failure, which both hammered the outlet and dragged the
            # health success-rate down relative to cached successes.
            # 30 min (not 24 h) so a transient block recovers on its own.
            cache.set(full_key, fallback, 1800)
            return await _served(fallback)
        # Nothing stored either — genuinely nothing to show.
        await db.record_extract_result(outlet, url, False, err.reason if err else "error")
        return {
            "error": "could not extract the article body",
            "reason": err.reason if err else "error",
        }

    # No LLM rewrite — show the publisher's body directly, just cleaned
    # of emojis / section headers / photo credits / mojibake. Cheaper,
    # faster, and the user explicitly asked for the original article
    # ("괜히 에이아이로 돌리지말고 그냥 바로 기사 보여줘").
    raw_title = html.unescape(reading.title or "")
    clean_title = _better_title(raw_title, fallback_title or "")
    paragraphs = _dedup_with_title(_split_paragraphs(reading.text), clean_title)
    result = {
        "url": url,
        # Where the body actually came from — for Google-News-sourced
        # items this is the publisher, not the Google interstitial, so
        # "open original" lands somewhere useful.
        "final_url": reading.final_url or url,
        "title": clean_title,
        "image": reading.image,
        "byline": reading.byline,
        "excerpt": html.unescape(reading.excerpt or ""),
        "lang": _detect_lang(clean_title),
        "word_count": len(reading.text.split()),
        "paragraphs": paragraphs,
    }
    cache.set(full_key, result, 86400)
    # Persist so a restart doesn't drop the scraped body.
    await db.save_reader(url, result)
    # Supervisor pass: enrich the card dek with prose from the
    # extracted body. _compose_dek_from_body skips title-as-paragraph
    # + subtitle stubs so EN cards get real body text, not header noise.
    try:
        combined = _compose_dek_from_body(
            result.get("paragraphs") or [],
            result.get("title") or "",
            result.get("lang"),
        )
        if combined and len(combined) >= 80:
            await db.update_article_summary(url, combined)
    except Exception as exc:
        print(f"[supervisor] dek backfill failed: {exc!r}", flush=True)
    # Image backfill — if the article entered the feed without an image
    # but reader.extract found one, stamp it onto the articles row so
    # the card shows a photo on the next refresh. Fixes "blank card,
    # picture appears when opened" complaint.
    try:
        if result.get("image"):
            await db.update_article_image(url, result["image"])
    except Exception as exc:
        print(f"[supervisor] image backfill failed: {exc!r}", flush=True)
    await db.bump_counter("reader_extracts_ok")
    return await _served(result)


# ── /api/lab — dashboard data ────────────────────────────────────────
@app.get("/api/lab")
async def lab_overview(request: Request):
    """Snapshot for the /lab dashboard: cache footprint, config, DB
    counters, outlet roster. Polled every few seconds. Admin-only."""
    await _require_admin(request)
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
async def lab_agent_runs(request: Request, limit: int = 50):
    await _require_admin(request)
    return {"runs": await db.recent_agent_runs(limit=min(max(limit, 1), 200))}


@app.get("/api/lab/settings")
async def lab_settings_get(request: Request):
    await _require_admin(request)
    return {
        "agent_interval_seconds": await db.get_setting(
            "agent_interval_seconds", AGENT_INTERVAL_SECONDS
        ),
        "workflow": await db.get_setting("workflow", registry.DEFAULT_WORKFLOW),
    }


@app.post("/api/lab/settings")
async def lab_settings_set(body: dict, request: Request):
    await _require_admin(request)
    """Persist user-tunable knobs. The refresh agent reads these on
    each tick so changes apply within one cycle."""
    out: dict = {}
    if "agent_interval_seconds" in body:
        v = int(body["agent_interval_seconds"])
        v = max(60, min(v, 6 * 3600))   # clamp 1 min … 6 h
        await db.set_setting("agent_interval_seconds", v)
        out["agent_interval_seconds"] = v
    if "workflow" in body:
        wf = str(body["workflow"])
        if wf in registry.WORKFLOWS:
            await db.set_setting("workflow", wf)
            out["workflow"] = wf
    return out


@app.get("/api/lab/agents")
async def lab_agents(request: Request):
    """Agent + workflow metadata for the lab dashboard. Admin-only."""
    await _require_admin(request)
    return {
        "agents": registry.agents_list(),
        "workflows": registry.workflow_list(),
        "active_workflow": await db.get_setting("workflow", registry.DEFAULT_WORKFLOW),
    }


@app.get("/lab")
async def lab_page(request: Request):
    """Lab HTML page — admin-only. Anonymous / non-admin visitors are
    bounced to /login (which renders the login form on top of the lab
    panel skeleton). We do NOT 401 here because that would render a
    raw browser error; instead we redirect, much nicer UX."""
    user = await auth.current_user(request)
    if not user or not user.get("is_admin"):
        return RedirectResponse(url="/login?next=/lab", status_code=302)
    return FileResponse(FRONTEND_DIR / "lab.html", headers=_NO_STORE_HEADERS)


@app.get("/status")
def status_page():
    """Public status page — per-source article-extraction health.

    Deliberately unauthenticated: it holds no user data and it is the
    page to reach for when the app looks broken, which is exactly when
    a login wall is least welcome.
    """
    return FileResponse(FRONTEND_DIR / "status.html", headers=_NO_STORE_HEADERS)


@app.get("/login")
async def login_page():
    """Tiny login page. Standalone HTML so we don't have to inline a
    form into the main feed UI."""
    return FileResponse(FRONTEND_DIR / "login.html", headers=_NO_STORE_HEADERS)


@app.get("/account")
async def account_page(request: Request):
    """User profile + account settings. Anonymous visitors are bounced
    to /login so the page itself never has to render an empty state."""
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/account", status_code=302)
    return FileResponse(FRONTEND_DIR / "account.html", headers=_NO_STORE_HEADERS)


def _row_to_card(r: dict) -> dict:
    """One `articles` row → the card shape the frontend renders.

    Single definition shared by /api/page and /api/search, so a new card
    field only has to be added in one place — the two used to drift.
    """
    return {
        "kind": "news",
        "url": r.get("url"),
        "title": r.get("title"),
        "image": r.get("image"),
        "image_source": r.get("image_source"),
        "outlet": r.get("outlet"),
        "category": r.get("category"),
        "lang": r.get("lang"),
        "dek": r.get("summary"),
        "why": r.get("why"),
        "tier": r.get("tier"),
        "premium": bool(r.get("premium")),
        "weight": float(r.get("weight") or 1.0),
        "quality": float(r.get("quality") or 0),
        "premium_body": r.get("premium_body"),
        # Card-level translation — populated on /api/translate success.
        # Presence drives the neon border + ✦한 badge.
        "title_ko": r.get("title_ko"),
        "dek_ko": r.get("dek_ko"),
        "translated_at": r.get("translated_at"),
        "ts": r.get("published_at"),
        "score": r.get("score") or 0,
        "tickers": [], "sparks": {},
    }


@app.get("/api/search")
async def api_search(q: str = "", n: int = 1, size: int = 24):
    """Search the archive by headline, dek or outlet.

    Returns the same card shape as /api/page so the frontend can render
    results with the existing card renderer instead of a parallel one.
    """
    q = (q or "").strip()
    n = max(1, n)
    size = max(1, min(size, 60))
    if len(q) < 2:
        return {
            "q": q, "page": n, "size": size,
            "total_pages": 1, "total_items": 0, "items": [],
            "error": "query too short" if q else None,
        }
    rows, total = await db.search_articles(q, limit=size, offset=(n - 1) * size)
    pages = max(1, (total + size - 1) // size)
    return {
        "q": q,
        "page": n,
        "size": size,
        "total_pages": pages,
        "total_items": total,
        "items": [_row_to_card(r) for r in rows],
    }


@app.get("/api/page")
async def api_page(
    n: int = 1, size: int = 13,
    cat: Optional[str] = None, premium: int = 0,
):
    """Lazy pagination — page 2+ pulls from the SQLite archive (active
    rows only). Total page count is hard-capped at MAX_VISIBLE_PAGES so
    the pager never grows past what the user actually wants to scroll
    through. Pass `premium=1` to filter to only premium-outlet articles."""
    MAX_VISIBLE_ARTICLES = 130
    n = max(1, n)
    size = max(1, min(size, 100))
    cat_clean = cat if cat and cat != "all" else None
    premium_only = bool(premium)
    total = min(
        await db.count_articles(cat=cat_clean), MAX_VISIBLE_ARTICLES,
    )
    pages = max(1, (total + size - 1) // size)
    n = min(n, pages)
    # ALL tab (no category, no premium filter) stays round-robin balanced
    # on every page so the Korean firehose can't bury the other channels
    # past page 1. Category / premium pages keep the flat ordering.
    if cat_clean is None and not premium_only:
        items = await db.list_articles_roundrobin(
            offset=(n - 1) * size, limit=size,
        )
    else:
        items = await db.list_articles(
            offset=(n - 1) * size,
            limit=size,
            cat=cat_clean,
            premium_only=premium_only,
        )
    converted = [_row_to_card(r) for r in items]
    return {"page": n, "size": size, "total_pages": pages, "total_items": total, "items": converted}


@app.get("/api/outlets")
async def outlets():
    return {"outlets": config.OUTLETS}


@app.get("/api/sources")
async def api_sources():
    """Grouped outlet list for the settings source picker. Each entry
    carries the premium flag + weight so the UI can show ★ next to
    premium publishers and offer 'enable only premium' as a quick action."""
    by_cat: dict[str, list[dict]] = {}
    for o in config.OUTLETS:
        cat = o.get("category") or "world"
        meta = config.outlet_meta(o["name"])
        by_cat.setdefault(cat, []).append({
            "name": o["name"],
            "lang": o.get("lang", "en"),
            "premium": meta["premium"],
            "weight": meta["weight"],
        })
    return {
        "categories": [
            {
                "key": k,
                "label": config.CATEGORY_LABELS.get(k, {}).get("en", k.title()),
                "label_ko": config.CATEGORY_LABELS.get(k, {}).get("ko", k),
                "outlets": sorted(v, key=lambda x: (-int(x["premium"]), x["name"])),
            }
            for k, v in by_cat.items()
        ],
        "total": sum(len(v) for v in by_cat.values()),
    }


@app.get("/api/user-prefs")
async def user_prefs_get():
    """Server-side store for user prefs. localStorage holds the same data
    on the client; this endpoint is the source of truth across devices."""
    return await db.get_setting("user_prefs", {
        "disabled_outlets": [],
        "hide_no_image": False,
        "premium_only": False,
        "page_size": 31,
        "auto_refresh": True,
        "workflow": "balanced",
    })


@app.post("/api/user-prefs")
async def user_prefs_set(body: dict):
    """Replace stored user prefs. Body is the full prefs object —
    frontend reads, edits, posts back."""
    safe = {
        "disabled_outlets": [str(s) for s in (body.get("disabled_outlets") or [])][:300],
        "hide_no_image": bool(body.get("hide_no_image")),
        "premium_only": bool(body.get("premium_only")),
        "page_size": max(10, min(100, int(body.get("page_size") or 31))),
        "auto_refresh": bool(body.get("auto_refresh", True)),
        "workflow": str(body.get("workflow") or "balanced")[:32],
    }
    await db.set_setting("user_prefs", safe)
    return safe


# ── Static frontend ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# Anti-stale-bundle headers on the entry HTML. Without these the browser
# happily serves index.html from cache, which means the user keeps loading
# old `app.js?v=49` even after we ship `?v=51` — and the translate-button
# fix never lands. Static assets under /static keep their long-lived
# default caching since the `?v=` query string changes per release.
_NO_STORE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html", headers=_NO_STORE_HEADERS)

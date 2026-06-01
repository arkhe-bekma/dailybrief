"""SQLite store — survives restarts so the og:image + trafilatura work
the agent has already paid for doesn't have to be repeated.

Plain stdlib sqlite3 + asyncio.to_thread (no new dep). One file at
backend/data/dailybrief.db. Tables:

- articles        : every story we've ingested (1 row per URL forever)
- reader_results  : /api/article extracted-body cache (1 row per URL)
- agent_runs      : every background agent tick — kind, status, ms
- settings        : key/value runtime knobs (agent interval etc.)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


DB_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DB_DIR / "dailybrief.db"


def _conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    return c


def _init_sync() -> None:
    """Three-phase init so a pre-existing DB without the new `score`
    column doesn't crash the index-creation step.
      1. CREATE TABLE IF NOT EXISTS (fresh installs get the full schema)
      2. ALTER TABLE migrations (existing installs gain new columns)
      3. CREATE INDEX IF NOT EXISTS (now every column the indexes
         reference is guaranteed to exist)"""
    with closing(_conn()) as c:
        # Phase 1: tables. Indexes split out below.
        c.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                url            TEXT PRIMARY KEY,
                title          TEXT,
                image          TEXT,
                outlet         TEXT,
                category       TEXT,
                lang           TEXT,
                summary        TEXT,
                score          INTEGER DEFAULT 0,
                published_at   TEXT,
                fetched_at     INTEGER NOT NULL,
                last_seen_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reader_results (
                url           TEXT PRIMARY KEY,
                payload_json  TEXT NOT NULL,
                fetched_at    INTEGER NOT NULL,
                last_used_at  INTEGER NOT NULL,
                use_count     INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL,
                started_at  INTEGER NOT NULL,
                ended_at    INTEGER,
                ok          INTEGER,
                note        TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS counters (
                key    TEXT PRIMARY KEY,
                n      INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Phase 2: migrations. Each ALTER is idempotent — ignore the
        # "duplicate column name" OperationalError on already-migrated DBs.
        # NB: these columns are API-visible labels: the front-end and any
        # downstream consumer can rely on them being present per article.
        for stmt in (
            "ALTER TABLE articles ADD COLUMN score INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN why TEXT",
            "ALTER TABLE articles ADD COLUMN image_source TEXT",
            "ALTER TABLE articles ADD COLUMN tier TEXT",
            # Premium-pick metadata. premium=1 → tier-1 outlet, weight is
            # a float multiplier the curator already applied. premium_body
            # is the longer-form summary the smart agents generate for
            # paywalled / high-quality stories.
            "ALTER TABLE articles ADD COLUMN premium INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN weight REAL DEFAULT 1.0",
            "ALTER TABLE articles ADD COLUMN premium_body TEXT",
            "ALTER TABLE articles ADD COLUMN quality REAL DEFAULT 0",
            # Card-level translation fields. Populated when /api/translate
            # successfully translates an article: title_ko gets the
            # translated headline, dek_ko gets the first ~280 chars of
            # the first translated paragraph. Their presence is what
            # tells the UI to render the neon border + ✦한 badge.
            "ALTER TABLE articles ADD COLUMN title_ko TEXT",
            "ALTER TABLE articles ADD COLUMN dek_ko TEXT",
            "ALTER TABLE articles ADD COLUMN translated_at INTEGER",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass

        # Phase 3: indexes (now that every column they reference exists).
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_articles_fetched   ON articles(fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_outlet    ON articles(outlet);
            CREATE INDEX IF NOT EXISTS idx_articles_category  ON articles(category);
            CREATE INDEX IF NOT EXISTS idx_articles_score     ON articles(score DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_premium   ON articles(premium DESC, score DESC);
            CREATE INDEX IF NOT EXISTS idx_reader_used        ON reader_results(last_used_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at DESC);
        """)


async def init() -> None:
    await asyncio.to_thread(_init_sync)


# ── articles ────────────────────────────────────────────────────────
def _upsert_article_sync(row: dict) -> None:
    now = int(time.time())
    with closing(_conn()) as c:
        c.execute("""
            INSERT INTO articles
              (url, title, image, outlet, category, lang, summary, score,
               why, image_source, tier,
               premium, weight, premium_body, quality,
               published_at, fetched_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title=excluded.title,
              image=COALESCE(excluded.image, articles.image),
              outlet=excluded.outlet,
              category=excluded.category,
              lang=excluded.lang,
              summary=excluded.summary,
              score=MAX(IFNULL(excluded.score, 0), IFNULL(articles.score, 0)),
              why=COALESCE(excluded.why, articles.why),
              image_source=COALESCE(excluded.image_source, articles.image_source),
              tier=COALESCE(excluded.tier, articles.tier),
              premium=MAX(IFNULL(excluded.premium, 0), IFNULL(articles.premium, 0)),
              weight=COALESCE(excluded.weight, articles.weight),
              premium_body=COALESCE(excluded.premium_body, articles.premium_body),
              quality=MAX(IFNULL(excluded.quality, 0), IFNULL(articles.quality, 0)),
              last_seen_at=excluded.last_seen_at
        """, (
            row.get("url"), row.get("title"), row.get("image"),
            row.get("outlet"), row.get("category"), row.get("lang"),
            row.get("summary"), int(row.get("score") or 0),
            row.get("why"), row.get("image_source"), row.get("tier"),
            int(bool(row.get("premium"))),
            float(row.get("weight") or 1.0),
            row.get("premium_body"),
            float(row.get("quality") or 0),
            row.get("published_at"),
            row.get("fetched_at") or now, now,
        ))


# ── page query (lazy / from-disk pagination) ───────────────────────
def _list_articles_sync(
    offset: int, limit: int, cat: str | None, premium_only: bool = False,
) -> list[dict]:
    args: list = []
    where_parts: list[str] = []
    if cat:
        where_parts.append("category = ?")
        args.append(cat)
    if premium_only:
        where_parts.append("premium = 1")
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    args += [limit, offset]
    with closing(_conn()) as c:
        rows = c.execute(
            f"SELECT url, title, image, outlet, category, lang, summary, score, "
            f"why, image_source, tier, premium, weight, premium_body, quality, "
            f"title_ko, dek_ko, translated_at, "
            f"published_at, fetched_at FROM articles {where} "
            f"ORDER BY premium DESC, score DESC, fetched_at DESC LIMIT ? OFFSET ?",
            args,
        ).fetchall()
        return [dict(r) for r in rows]


def _count_articles_sync(cat: str | None) -> int:
    args: list = []
    where = ""
    if cat:
        where = "WHERE category = ?"
        args.append(cat)
    with closing(_conn()) as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM articles {where}", args).fetchone()["n"]


async def list_articles(
    offset: int, limit: int, cat: str | None = None, premium_only: bool = False,
) -> list[dict]:
    return await asyncio.to_thread(
        _list_articles_sync, offset, limit, cat, premium_only,
    )


async def count_articles(cat: str | None = None) -> int:
    return await asyncio.to_thread(_count_articles_sync, cat)


def _upsert_articles_batch_sync(rows: list[dict]) -> int:
    """One connection, one transaction, N inserts. The single-row
    helper above opens a new connection per row — fine for a handful
    of writes but ruinous on the baseline-upsert path that touches
    1000+ articles per /api/brief. Slow disk on Lightsail was queuing
    write locks until uvicorn timed out and Caddy returned 502."""
    if not rows:
        return 0
    now = int(time.time())
    with closing(_conn()) as c:
        # Explicit BEGIN/COMMIT around the batch. isolation_level=None
        # on the connection means autocommit; wrap manually to batch.
        c.execute("BEGIN")
        try:
            for row in rows:
                c.execute("""
                    INSERT INTO articles
                      (url, title, image, outlet, category, lang, summary, score,
                       why, image_source, tier,
                       premium, weight, premium_body, quality,
                       published_at, fetched_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                      title=excluded.title,
                      image=COALESCE(excluded.image, articles.image),
                      outlet=excluded.outlet,
                      category=excluded.category,
                      lang=excluded.lang,
                      summary=COALESCE(NULLIF(excluded.summary, ''), articles.summary),
                      score=MAX(IFNULL(excluded.score, 0), IFNULL(articles.score, 0)),
                      why=COALESCE(excluded.why, articles.why),
                      image_source=COALESCE(excluded.image_source, articles.image_source),
                      tier=COALESCE(excluded.tier, articles.tier),
                      premium=MAX(IFNULL(excluded.premium, 0), IFNULL(articles.premium, 0)),
                      weight=COALESCE(excluded.weight, articles.weight),
                      premium_body=COALESCE(excluded.premium_body, articles.premium_body),
                      quality=MAX(IFNULL(excluded.quality, 0), IFNULL(articles.quality, 0)),
                      last_seen_at=excluded.last_seen_at
                """, (
                    row.get("url"), row.get("title"), row.get("image"),
                    row.get("outlet"), row.get("category"), row.get("lang"),
                    row.get("summary"), int(row.get("score") or 0),
                    row.get("why"), row.get("image_source"), row.get("tier"),
                    int(bool(row.get("premium"))),
                    float(row.get("weight") or 1.0),
                    row.get("premium_body"),
                    float(row.get("quality") or 0),
                    row.get("published_at"),
                    row.get("fetched_at") or now, now,
                ))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(rows)


async def upsert_articles(rows: list[dict]) -> int:
    """Batched upsert — one connection, one transaction. Much cheaper
    than the per-row helper on slow disks."""
    if not rows:
        return 0
    return await asyncio.to_thread(_upsert_articles_batch_sync, rows)


# ── Card-level translation persistence ─────────────────────────────
def _save_card_translation_sync(
    url: str, title_ko: str, dek_ko: str,
) -> bool:
    """Stamp the card-level translation onto the articles row. Returns
    True if a row was updated, False if no row existed (article was
    only in reader_results / not in the main articles table)."""
    now = int(time.time())
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles SET title_ko = ?, dek_ko = ?, translated_at = ? "
            "WHERE url = ?",
            (title_ko, dek_ko, now, url),
        )
        return cur.rowcount > 0


async def save_card_translation(
    url: str, title_ko: str, dek_ko: str,
) -> bool:
    return await asyncio.to_thread(
        _save_card_translation_sync, url, title_ko, dek_ko,
    )


def _get_card_translation_sync(url: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT title_ko, dek_ko, translated_at FROM articles "
            "WHERE url = ? AND title_ko IS NOT NULL",
            (url,),
        ).fetchone()
        return dict(row) if row else None


async def get_card_translation(url: str) -> dict | None:
    return await asyncio.to_thread(_get_card_translation_sync, url)


def _get_card_translations_batch_sync(urls: list[str]) -> dict[str, dict]:
    if not urls:
        return {}
    out: dict[str, dict] = {}
    with closing(_conn()) as c:
        # IN-clause batched 200 at a time so a giant url list doesn't
        # blow past SQLite's 999-parameter limit.
        for i in range(0, len(urls), 200):
            chunk = urls[i:i + 200]
            placeholders = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT url, title_ko, dek_ko, translated_at "
                f"FROM articles WHERE title_ko IS NOT NULL "
                f"AND url IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["url"]] = dict(r)
    return out


async def get_card_translations(urls: list[str]) -> dict[str, dict]:
    """Batch lookup. Returns {url: {title_ko, dek_ko, translated_at}}
    for any URLs in the input that actually have a saved translation."""
    return await asyncio.to_thread(_get_card_translations_batch_sync, urls)


def _update_article_summary_sync(url: str, summary: str) -> bool:
    """Refresh articles.summary with a richer body excerpt (typically
    the first paragraph of a successful reader extract). Only updates
    when the new text is meaningfully longer than the existing one —
    avoids overwriting curator-supplied summaries with shorter junk."""
    if not summary:
        return False
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles "
            "SET summary = ? "
            "WHERE url = ? AND (summary IS NULL OR length(summary) < length(?))",
            (summary, url, summary),
        )
        return cur.rowcount > 0


async def update_article_summary(url: str, summary: str) -> bool:
    return await asyncio.to_thread(_update_article_summary_sync, url, summary)


# ── reader cache ────────────────────────────────────────────────────
def _get_reader_sync(url: str) -> dict | None:
    now = int(time.time())
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT payload_json FROM reader_results WHERE url=?", (url,)
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE reader_results SET last_used_at=?, use_count=use_count+1 WHERE url=?",
            (now, url),
        )
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None


async def get_reader(url: str) -> dict | None:
    return await asyncio.to_thread(_get_reader_sync, url)


def _save_reader_sync(url: str, payload: dict) -> None:
    now = int(time.time())
    with closing(_conn()) as c:
        c.execute("""
            INSERT INTO reader_results (url, payload_json, fetched_at, last_used_at, use_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(url) DO UPDATE SET
              payload_json=excluded.payload_json,
              fetched_at=excluded.fetched_at,
              last_used_at=excluded.last_used_at
        """, (url, json.dumps(payload, ensure_ascii=False), now, now))


async def save_reader(url: str, payload: dict) -> None:
    await asyncio.to_thread(_save_reader_sync, url, payload)


# ── agent runs ──────────────────────────────────────────────────────
def _log_agent_run_sync(kind: str, started_at: float, ended_at: float | None, ok: bool, note: str) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO agent_runs (kind, started_at, ended_at, ok, note) VALUES (?, ?, ?, ?, ?)",
            (kind, int(started_at), int(ended_at) if ended_at else None, 1 if ok else 0, note[:300]),
        )
        # Trim to most recent 500 to keep the table tidy.
        c.execute(
            "DELETE FROM agent_runs WHERE id IN ("
            "  SELECT id FROM agent_runs ORDER BY id DESC LIMIT -1 OFFSET 500"
            ")"
        )


async def log_agent_run(kind: str, started_at: float, ended_at: float | None, ok: bool, note: str = "") -> None:
    await asyncio.to_thread(_log_agent_run_sync, kind, started_at, ended_at, ok, note)


def _recent_agent_runs_sync(limit: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT id, kind, started_at, ended_at, ok, note FROM agent_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


async def recent_agent_runs(limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_recent_agent_runs_sync, limit)


# ── settings ────────────────────────────────────────────────────────
def _get_setting_sync(key: str, default: Any = None) -> Any:
    with closing(_conn()) as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default


async def get_setting(key: str, default: Any = None) -> Any:
    return await asyncio.to_thread(_get_setting_sync, key, default)


def _set_setting_sync(key: str, value: Any) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


async def set_setting(key: str, value: Any) -> None:
    await asyncio.to_thread(_set_setting_sync, key, value)


# ── counters ───────────────────────────────────────────────────────
def _bump_counter_sync(key: str, by: int = 1) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO counters(key, n) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET n=n+excluded.n",
            (key, by),
        )


async def bump_counter(key: str, by: int = 1) -> None:
    await asyncio.to_thread(_bump_counter_sync, key, by)


# ── stats (for /api/lab) ───────────────────────────────────────────
def _stats_sync() -> dict:
    with closing(_conn()) as c:
        articles_total = c.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        articles_24h = c.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE fetched_at > ?",
            (int(time.time()) - 86400,),
        ).fetchone()["n"]
        reader_total = c.execute("SELECT COUNT(*) AS n FROM reader_results").fetchone()["n"]
        reader_cache_hits = c.execute(
            "SELECT COALESCE(SUM(use_count - 1), 0) AS n FROM reader_results"
        ).fetchone()["n"]
        # Pull per-outlet counts + last-fetched from the articles table.
        # Wrapped in try/except so a hiccup here never 500s /api/lab
        # (the prior version took the whole service down on prod).
        outlets: list[dict] = []
        try:
            rows = c.execute(
                "SELECT outlet, COUNT(*) AS articles, "
                "MAX(fetched_at) AS last_fetched "
                "FROM articles GROUP BY outlet"
            ).fetchall()
            db_outlets: dict = {}
            for r in rows:
                name = r["outlet"]
                if not name:
                    continue
                db_outlets[name] = {
                    "articles": r["articles"] or 0,
                    "last_fetched": r["last_fetched"],
                }
            # Roster = every CONFIGURED outlet, with 0 for the ones that
            # have never produced an article. Lets the user spot dead
            # feeds in the lab dashboard.
            from backend import config as _cfg
            seen: set = set()
            for o in getattr(_cfg, "OUTLETS", []):
                name = o.get("name")
                if not name:
                    continue
                row = db_outlets.get(name, {})
                try:
                    meta = _cfg.outlet_meta(name)
                except Exception:
                    meta = {"premium": False, "weight": 1.0}
                outlets.append({
                    "outlet": name,
                    "category": o.get("category"),
                    "lang": o.get("lang", "en"),
                    "premium": bool(meta.get("premium")),
                    "weight": float(meta.get("weight") or 1.0),
                    "articles": row.get("articles", 0),
                    "last_fetched": row.get("last_fetched"),
                    "configured": True,
                })
                seen.add(name)
            # Append DB-only outlets (renamed/retired in config) so the
            # roster keeps history rather than silently dropping them.
            for name, row in db_outlets.items():
                if name in seen:
                    continue
                outlets.append({
                    "outlet": name,
                    "category": None,
                    "lang": "?",
                    "premium": False,
                    "weight": 1.0,
                    "articles": row.get("articles", 0),
                    "last_fetched": row.get("last_fetched"),
                    "configured": False,
                })
            outlets.sort(key=lambda r: (-(r.get("articles") or 0), r.get("outlet") or ""))
        except Exception as exc:
            # Never let the roster path 500 the whole lab endpoint.
            print(f"[db] stats outlets path failed: {exc!r}", flush=True)
            outlets = []
        counters = [dict(r) for r in c.execute("SELECT key, n FROM counters").fetchall()]
        size_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {
            "articles_total": articles_total,
            "articles_24h": articles_24h,
            "reader_cache_size": reader_total,
            "reader_cache_hits": reader_cache_hits,
            "outlets": outlets,
            "counters": {c["key"]: c["n"] for c in counters},
            "db_bytes": size_bytes,
        }


async def stats() -> dict:
    return await asyncio.to_thread(_stats_sync)

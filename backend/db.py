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
    with closing(_conn()) as c:
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
            CREATE INDEX IF NOT EXISTS idx_articles_fetched   ON articles(fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_outlet    ON articles(outlet);
            CREATE INDEX IF NOT EXISTS idx_articles_category  ON articles(category);
            CREATE INDEX IF NOT EXISTS idx_articles_score     ON articles(score DESC);

            CREATE TABLE IF NOT EXISTS reader_results (
                url           TEXT PRIMARY KEY,
                payload_json  TEXT NOT NULL,
                fetched_at    INTEGER NOT NULL,
                last_used_at  INTEGER NOT NULL,
                use_count     INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_reader_used ON reader_results(last_used_at DESC);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL,
                started_at  INTEGER NOT NULL,
                ended_at    INTEGER,
                ok          INTEGER,
                note        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at DESC);

            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS counters (
                key    TEXT PRIMARY KEY,
                n      INTEGER NOT NULL DEFAULT 0
            );
        """)


async def init() -> None:
    await asyncio.to_thread(_init_sync)
    # Online schema migrations — safe to re-run, ignore if column already exists.
    def _migrate():
        with closing(_conn()) as c:
            try:
                c.execute("ALTER TABLE articles ADD COLUMN score INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
    await asyncio.to_thread(_migrate)


# ── articles ────────────────────────────────────────────────────────
def _upsert_article_sync(row: dict) -> None:
    now = int(time.time())
    with closing(_conn()) as c:
        c.execute("""
            INSERT INTO articles
              (url, title, image, outlet, category, lang, summary, score, published_at, fetched_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title=excluded.title,
              image=COALESCE(excluded.image, articles.image),
              outlet=excluded.outlet,
              category=excluded.category,
              lang=excluded.lang,
              summary=excluded.summary,
              score=MAX(IFNULL(excluded.score, 0), IFNULL(articles.score, 0)),
              last_seen_at=excluded.last_seen_at
        """, (
            row.get("url"), row.get("title"), row.get("image"),
            row.get("outlet"), row.get("category"), row.get("lang"),
            row.get("summary"), int(row.get("score") or 0),
            row.get("published_at"),
            row.get("fetched_at") or now, now,
        ))


# ── page query (lazy / from-disk pagination) ───────────────────────
def _list_articles_sync(offset: int, limit: int, cat: str | None) -> list[dict]:
    args: list = []
    where = ""
    if cat:
        where = "WHERE category = ?"
        args.append(cat)
    args += [limit, offset]
    with closing(_conn()) as c:
        rows = c.execute(
            f"SELECT url, title, image, outlet, category, lang, summary, score, "
            f"published_at, fetched_at FROM articles {where} "
            f"ORDER BY score DESC, fetched_at DESC LIMIT ? OFFSET ?",
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


async def list_articles(offset: int, limit: int, cat: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_list_articles_sync, offset, limit, cat)


async def count_articles(cat: str | None = None) -> int:
    return await asyncio.to_thread(_count_articles_sync, cat)


async def upsert_articles(rows: list[dict]) -> int:
    if not rows:
        return 0
    def _go():
        for r in rows:
            _upsert_article_sync(r)
        return len(rows)
    return await asyncio.to_thread(_go)


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
        outlets = [
            dict(r) for r in c.execute("""
                SELECT outlet, COUNT(*) AS articles,
                       MAX(fetched_at) AS last_fetched
                FROM articles GROUP BY outlet
                ORDER BY articles DESC
            """).fetchall()
        ]
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

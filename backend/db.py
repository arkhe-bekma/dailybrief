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

            CREATE TABLE IF NOT EXISTS api_calls (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                provider        TEXT NOT NULL,
                purpose         TEXT NOT NULL,
                input_tokens    INTEGER DEFAULT 0,
                output_tokens   INTEGER DEFAULT 0,
                cost_usd        REAL DEFAULT 0,
                success         INTEGER NOT NULL DEFAULT 1,
                note            TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_api_calls_provider ON api_calls(provider, ts DESC);

            -- User-driven removals. When the user clicks × on a card
            -- and picks a reason, the URL lands here so RSS re-ingest
            -- can never bring it back. Reason is a short slug
            -- (quality / irrelevant / incomplete / broken / duplicate
            -- / misleading / other) for later analysis.
            CREATE TABLE IF NOT EXISTS blocked_urls (
                url        TEXT PRIMARY KEY,
                reason     TEXT,
                blocked_at INTEGER NOT NULL,
                title      TEXT,
                outlet     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_blocked_at ON blocked_urls(blocked_at DESC);

            -- Per-attempt log of reader extractions. One row per
            -- /api/article miss-path attempt, so the health view can
            -- answer "which outlets stopped opening, and since when".
            -- `reason` is a stable slug from reader.ExtractError:
            -- paywall / blocked / notfound / timeout / empty / error,
            -- empty string on success.
            CREATE TABLE IF NOT EXISTS extract_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                outlet  TEXT NOT NULL DEFAULT '',
                url     TEXT NOT NULL,
                ok      INTEGER NOT NULL,
                reason  TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_extract_ts ON extract_log(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_extract_outlet ON extract_log(outlet, ts DESC);
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
            # Quality-validation flags. validated:
            #   0  = not yet checked (initial state, waiting on the worker)
            #   1  = passes validator.validate → safe to show on the feed
            #  -1  = failed validation (see validation_reason)
            # The body + image have been confirmed in reader_results by the
            # time validated=1, so the article is link-rot proof.
            "ALTER TABLE articles ADD COLUMN validated INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN validation_reason TEXT",
            "ALTER TABLE articles ADD COLUMN validated_at INTEGER",
            # Archive flag — articles older than ~7 days flip to 1 so
            # the "active" feed stays small + fast. Archived rows
            # aren't purged: they stay readable via /api/page&archived=1
            # and the modal still serves them from reader_results.
            "ALTER TABLE articles ADD COLUMN archived INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN archived_at INTEGER",
            # Ranking-department fields (backend/agent/ranker.py):
            #   dup_of        = survivor URL this row was collapsed into
            #                   (NULL = survivor / not yet ranked → visible)
            #   corroboration = how many outlets carried the same story
            #   ranked_at     = last time the ranker scored this row
            "ALTER TABLE articles ADD COLUMN dup_of TEXT",
            "ALTER TABLE articles ADD COLUMN corroboration INTEGER DEFAULT 0",
            "ALTER TABLE articles ADD COLUMN ranked_at INTEGER",
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
            CREATE INDEX IF NOT EXISTS idx_articles_validated ON articles(validated, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_archived  ON articles(archived, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_articles_active    ON articles(archived, validated, fetched_at DESC);
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
            # tier is NOT NULL DEFAULT 'hot' in the schema, but the
            # default only fires when the column is omitted from the
            # INSERT list — passing None still violates the constraint.
            # rss-ingest never sets tier, so fall back to 'hot' here.
            row.get("why"), row.get("image_source"), row.get("tier") or "hot",
            int(bool(row.get("premium"))),
            float(row.get("weight") or 1.0),
            row.get("premium_body"),
            float(row.get("quality") or 0),
            row.get("published_at"),
            row.get("fetched_at") or now, now,
        ))


# ── page query (lazy / from-disk pagination) ───────────────────────
# All article-listing paths default to the ACTIVE set (archived = 0).
# Lab / debug callers can pass include_archived=True to see everything.
def _list_articles_sync(
    offset: int, limit: int, cat: str | None, premium_only: bool = False,
    validated_only: bool = True, include_archived: bool = False,
) -> list[dict]:
    args: list = []
    where_parts: list[str] = []
    if not include_archived:
        where_parts.append("archived = 0")
    if cat:
        where_parts.append("category = ?")
        args.append(cat)
    if premium_only:
        where_parts.append("premium = 1")
    # Hide cross-outlet duplicates the ranker collapsed. Unranked rows
    # have dup_of NULL, so nothing disappears before the first rank pass.
    where_parts.append("(dup_of IS NULL OR dup_of = '')")
    if validated_only:
        # Default mode: hide articles the validator has confirmed
        # broken (validated = -1). Pending (0) and passed (1) both
        # flow through so a fresh deploy isn't an empty feed while
        # the worker chews through the backlog. Pass validated_only
        # =False to see the full archive (lab debug).
        where_parts.append("validated != -1")
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


def _search_articles_sync(q: str, limit: int, offset: int) -> tuple[list[dict], int]:
    """Substring search over headline, dek and outlet.

    LIKE rather than FTS5 on purpose: the corpus is a few thousand rows,
    the box is a 1 GB instance, and FTS5 tokenisation splits Korean badly
    enough that 한글 queries would miss. LIKE with NOCASE handles both
    scripts and needs no extra table to keep in sync.

    Ranking is deliberate: a title hit beats a dek hit beats an outlet
    hit, then by recency — so searching an outlet name doesn't bury the
    story that's actually about it.
    """
    needle = f"%{q}%"
    args = [needle, needle, needle]
    where = (
        "WHERE archived = 0 AND (dup_of IS NULL OR dup_of = '') "
        "AND validated != -1 "
        "AND (title LIKE ? COLLATE NOCASE "
        "  OR summary LIKE ? COLLATE NOCASE "
        "  OR outlet LIKE ? COLLATE NOCASE)"
    )
    with closing(_conn()) as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM articles {where}", args
        ).fetchone()["n"]
        rows = c.execute(
            f"SELECT url, title, image, outlet, category, lang, summary, score, "
            f"why, image_source, tier, premium, weight, premium_body, quality, "
            f"title_ko, dek_ko, translated_at, published_at, fetched_at, "
            f"CASE WHEN title LIKE ? COLLATE NOCASE THEN 0 "
            f"     WHEN summary LIKE ? COLLATE NOCASE THEN 1 "
            f"     ELSE 2 END AS match_rank "
            f"FROM articles {where} "
            f"ORDER BY match_rank ASC, premium DESC, fetched_at DESC "
            f"LIMIT ? OFFSET ?",
            [needle, needle] + args + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


async def search_articles(
    q: str, limit: int = 40, offset: int = 0,
) -> tuple[list[dict], int]:
    """Search headlines/deks/outlets. Returns (rows, total_matches)."""
    q = (q or "").strip()
    if len(q) < 2:
        return [], 0
    return await asyncio.to_thread(_search_articles_sync, q, limit, offset)


def _count_articles_sync(cat: str | None, include_archived: bool = False) -> int:
    args: list = []
    where_parts: list[str] = ["(dup_of IS NULL OR dup_of = '')"]
    if not include_archived:
        where_parts.append("archived = 0")
    if cat:
        where_parts.append("category = ?")
        args.append(cat)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    with closing(_conn()) as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM articles {where}", args).fetchone()["n"]


async def list_articles(
    offset: int, limit: int, cat: str | None = None, premium_only: bool = False,
    validated_only: bool = True, include_archived: bool = False,
) -> list[dict]:
    return await asyncio.to_thread(
        _list_articles_sync, offset, limit, cat, premium_only, validated_only,
        include_archived,
    )


# ── ALL-feed round-robin listing ────────────────────────────────────
# The ALL tab must stay category-balanced on EVERY page, not just page 1.
# Flat ordering let the Korean firehose (14k+ 'korea' rows) bury every
# other category from page 2 on. This paginates a round-robin: rank each
# article within its category, then emit rank-1 of every category (in
# chip order) before any rank-2. Ordering WITHIN a category is
# premium → score → published_at, so it's stable across refreshes AND
# becomes importance-first automatically once the ranker populates
# premium/score (today they're all 0, so it's newest-first per cat).
def _list_articles_roundrobin_sync(offset: int, limit: int) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute(
            """
            WITH ranked AS (
              SELECT url, title, image, outlet, category, lang, summary, score,
                     why, image_source, tier, premium, weight, premium_body,
                     quality, title_ko, dek_ko, translated_at, published_at,
                     fetched_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY category
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
              WHERE archived = 0 AND validated != -1
                AND (dup_of IS NULL OR dup_of = '')
            )
            SELECT url, title, image, outlet, category, lang, summary, score,
                   why, image_source, tier, premium, weight, premium_body,
                   quality, title_ko, dek_ko, translated_at, published_at,
                   fetched_at
            FROM ranked
            ORDER BY cat_rank ASC, cat_order ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


async def list_articles_roundrobin(offset: int, limit: int) -> list[dict]:
    """Paginated round-robin ALL feed straight from SQLite."""
    return await asyncio.to_thread(_list_articles_roundrobin_sync, offset, limit)


def _count_articles_validated_sync(cat: str | None, include_archived: bool = False) -> int:
    # Same semantics as the listing path: hide validated=-1 + archived +
    # collapsed duplicates.
    args: list = []
    where_parts = ["validated != -1", "(dup_of IS NULL OR dup_of = '')"]
    if not include_archived:
        where_parts.append("archived = 0")
    if cat:
        where_parts.append("category = ?")
        args.append(cat)
    where = "WHERE " + " AND ".join(where_parts)
    with closing(_conn()) as c:
        return c.execute(
            f"SELECT COUNT(*) AS n FROM articles {where}", args,
        ).fetchone()["n"]


async def count_articles(
    cat: str | None = None, validated_only: bool = True,
    include_archived: bool = False,
) -> int:
    if validated_only:
        return await asyncio.to_thread(
            _count_articles_validated_sync, cat, include_archived,
        )
    return await asyncio.to_thread(_count_articles_sync, cat, include_archived)


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
        # Drop anything the user has explicitly blocked. Each blocked
        # URL lives in `blocked_urls`; filter the incoming batch in
        # one cheap pass so re-ingest can never resurrect a removed
        # article on the next RSS sweep.
        blocked = {r["url"] for r in c.execute("SELECT url FROM blocked_urls").fetchall()}
        if blocked:
            rows = [r for r in rows if r.get("url") not in blocked]
            if not rows:
                return 0
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
                    # tier NOT NULL DEFAULT 'hot': passing None violates the
                    # constraint (the DEFAULT only fires if the column is
                    # omitted). Caller never sets tier on the rss-ingest path
                    # → batch was crashing every 30 min, blocking ALL new
                    # article fetches. Fall back here.
                    row.get("why"), row.get("image_source"), row.get("tier") or "hot",
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


def _get_validation_states_sync(urls: list[str]) -> dict[str, int]:
    """Returns {url: validated_status} for every requested URL that has
    a row in articles. Used by /api/brief to keep "not yet validated"
    and "failed" articles off the feed."""
    if not urls:
        return {}
    out: dict[str, int] = {}
    with closing(_conn()) as c:
        for i in range(0, len(urls), 200):
            chunk = urls[i:i + 200]
            placeholders = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT url, validated FROM articles WHERE url IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                out[r["url"]] = r["validated"]
    return out


async def get_validation_states(urls: list[str]) -> dict[str, int]:
    return await asyncio.to_thread(_get_validation_states_sync, urls)


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


def _force_update_summary_sync(url: str, summary: str) -> bool:
    """Force-overwrite articles.summary regardless of existing length.
    Used by the resummary worker when we KNOW the new text comes from
    the extracted body — user wants the real body as the dek, not the
    publisher's RSS one-liner."""
    if not summary:
        return False
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles SET summary = ? WHERE url = ?",
            (summary, url),
        )
        return cur.rowcount > 0


async def force_update_summary(url: str, summary: str) -> bool:
    return await asyncio.to_thread(_force_update_summary_sync, url, summary)


def _list_resummary_candidates_sync(limit: int) -> list[dict]:
    """Articles where we have a reader_results body but the card
    summary is still shorter than what the body would give us. These
    are the rows the resummary worker should rewrite."""
    with closing(_conn()) as c:
        rows = c.execute(
            """
            SELECT a.url AS url, a.title AS title, a.lang AS lang,
                   COALESCE(LENGTH(a.summary), 0) AS summary_len
            FROM articles a
            INNER JOIN reader_results r ON r.url = a.url
            WHERE COALESCE(LENGTH(a.summary), 0) < 240
            ORDER BY a.fetched_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


async def list_resummary_candidates(limit: int = 12) -> list[dict]:
    return await asyncio.to_thread(_list_resummary_candidates_sync, limit)


def _get_article_sync(url: str) -> dict | None:
    """Single-row lookup so the reader endpoint can grab the RSS
    headline as a fallback when trafilatura returns a sidebar label."""
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT url, title, image, outlet, category, lang, summary "
            "FROM articles WHERE url = ?",
            (url,),
        ).fetchone()
        return dict(row) if row else None


async def get_article(url: str) -> dict | None:
    return await asyncio.to_thread(_get_article_sync, url)


def _reset_validation_sync() -> int:
    """Bulk-reset every article back to `pending` so the validation
    worker re-runs the latest heuristics against the entire archive.
    Returns the row count touched."""
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles "
            "SET validated = 0, validation_reason = NULL, validated_at = NULL"
        )
        return cur.rowcount


async def reset_validation() -> int:
    return await asyncio.to_thread(_reset_validation_sync)


def _archive_old_articles_sync(days_old: int) -> int:
    """Flip `archived = 1` on every article whose `fetched_at` is older
    than `days_old` days. Articles aren't deleted — they stay queryable
    via include_archived=True paths (lab, audit). Returns the count
    flipped on this call."""
    cutoff = int(time.time()) - days_old * 86_400
    now = int(time.time())
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles SET archived = 1, archived_at = ? "
            "WHERE archived = 0 AND fetched_at < ?",
            (now, cutoff),
        )
        return cur.rowcount


async def archive_old_articles(days_old: int = 7) -> int:
    return await asyncio.to_thread(_archive_old_articles_sync, days_old)


def _unarchive_all_sync() -> int:
    """Restore every archived article back into the active pool. Used
    by the lab when the operator wants to surface the long tail again."""
    now = int(time.time())
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles SET archived = 0, archived_at = NULL WHERE archived = 1"
        )
        return cur.rowcount


async def unarchive_all() -> int:
    return await asyncio.to_thread(_unarchive_all_sync)


def _archive_stats_sync() -> dict:
    """Counts of active vs archived for the lab dashboard."""
    with closing(_conn()) as c:
        active = c.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE archived = 0"
        ).fetchone()["n"]
        archived = c.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE archived = 1"
        ).fetchone()["n"]
        # Oldest active article so the operator can spot when they
        # should archive again.
        oldest = c.execute(
            "SELECT MIN(fetched_at) AS t FROM articles WHERE archived = 0"
        ).fetchone()["t"]
        return {"active": active, "archived": archived, "oldest_active_fetched_at": oldest}


async def archive_stats() -> dict:
    return await asyncio.to_thread(_archive_stats_sync)


def _factory_reset_sync() -> dict:
    """Nuke the article inventory back to empty. Drops every row from
    articles, blocked_urls, and reader_results. Keeps users + sessions
    intact so the admin doesn't get logged out. Followed by VACUUM so
    the file actually shrinks on disk.

    Used when the DB has grown unwieldy + the operator wants to start
    from a clean slate. Next /api/brief refill cycle will reseed from
    live RSS feeds.
    """
    with closing(_conn()) as c:
        a = c.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        r = c.execute("SELECT COUNT(*) AS n FROM reader_results").fetchone()["n"]
        b = c.execute("SELECT COUNT(*) AS n FROM blocked_urls").fetchone()["n"]
        c.execute("DELETE FROM articles")
        c.execute("DELETE FROM reader_results")
        c.execute("DELETE FROM blocked_urls")
        c.execute("VACUUM")
    return {"removed_articles": a, "removed_reader_results": r, "removed_blocked": b}


async def factory_reset() -> dict:
    return await asyncio.to_thread(_factory_reset_sync)


def _purge_failed_articles_sync() -> int:
    """Drop every article currently marked validated = -1 along with
    its cached reader_results body. Used by the admin rebuild endpoint
    after the validator passes over the archive with new heuristics."""
    with closing(_conn()) as c:
        dead = [r["url"] for r in c.execute(
            "SELECT url FROM articles WHERE validated = -1"
        ).fetchall()]
        if not dead:
            return 0
        c.executemany("DELETE FROM articles WHERE url = ?", [(u,) for u in dead])
        c.executemany("DELETE FROM reader_results WHERE url = ?", [(u,) for u in dead])
        return len(dead)


async def purge_failed_articles() -> int:
    return await asyncio.to_thread(_purge_failed_articles_sync)


def _delete_article_by_user_sync(
    url: str, reason: str, note: str | None = None,
) -> dict:
    """User pressed × on a card. Drop the article row + cached reader
    body, then add the URL to blocked_urls so a future RSS sweep
    can't pull the same story back. Returns counts."""
    now = int(time.time())
    with closing(_conn()) as c:
        meta = c.execute(
            "SELECT title, outlet FROM articles WHERE url = ?", (url,)
        ).fetchone()
        title = meta["title"] if meta else None
        outlet = meta["outlet"] if meta else None
        a = c.execute("DELETE FROM articles WHERE url = ?", (url,)).rowcount
        r = c.execute("DELETE FROM reader_results WHERE url = ?", (url,)).rowcount
        c.execute(
            "INSERT INTO blocked_urls (url, reason, blocked_at, title, outlet) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  reason=excluded.reason, blocked_at=excluded.blocked_at",
            (url, (reason or "")[:32] + (f"|{note[:200]}" if note else ""),
             now, title, outlet),
        )
    return {"articles_deleted": a, "readers_deleted": r, "blocked": 1}


async def delete_article_by_user(
    url: str, reason: str, note: str | None = None,
) -> dict:
    return await asyncio.to_thread(_delete_article_by_user_sync, url, reason, note)


def _is_blocked_sync(url: str) -> bool:
    if not url:
        return False
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT 1 FROM blocked_urls WHERE url = ?", (url,)
        ).fetchone()
        return bool(row)


async def is_blocked(url: str) -> bool:
    return await asyncio.to_thread(_is_blocked_sync, url)


def _blocked_url_set_sync() -> set[str]:
    """Bulk read of every blocked URL. Cheap — the table is rarely
    bigger than a few hundred entries. Used by upsert_articles to
    filter incoming RSS before write, and by /api/brief to drop any
    already-cached items that the user has since blocked."""
    with closing(_conn()) as c:
        rows = c.execute("SELECT url FROM blocked_urls").fetchall()
        return {r["url"] for r in rows}


async def blocked_url_set() -> set[str]:
    return await asyncio.to_thread(_blocked_url_set_sync)


def _purge_bad_title_readers_sync(needles: list[str]) -> int:
    """Drop reader_results entries whose stored title matches one of
    the generic boilerplate labels. Forces a fresh trafilatura pass
    the next time the article is opened so the title-fallback logic
    has a chance to run."""
    if not needles:
        return 0
    deleted = 0
    with closing(_conn()) as c:
        for needle in needles:
            cur = c.execute(
                "DELETE FROM reader_results "
                "WHERE LOWER(json_extract(payload_json, '$.title')) "
                "      LIKE LOWER(?)",
                (f"%{needle}%",),
            )
            deleted += cur.rowcount
    return deleted


async def purge_bad_title_readers(needles: list[str]) -> int:
    return await asyncio.to_thread(_purge_bad_title_readers_sync, needles)


def _update_article_image_sync(url: str, image: str) -> bool:
    """Stamp the article's image URL onto the articles row — only when
    there isn't one yet. Used by /api/article so a successful
    reader.extract gives blank-image cards a photo on the next refresh."""
    if not image:
        return False
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles SET image = ? "
            "WHERE url = ? AND (image IS NULL OR image = '')",
            (image, url),
        )
        return cur.rowcount > 0


async def update_article_image(url: str, image: str) -> bool:
    return await asyncio.to_thread(_update_article_image_sync, url, image)


# ── Validation worker helpers ──────────────────────────────────────
def _list_unvalidated_sync(limit: int) -> list[dict]:
    """Pick the next batch of articles the worker should check. Skips
    archived rows so the worker doesn't waste cycles on stale content.
    Ordered by recency so the freshest items get validated first."""
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT url, title, image, outlet, category, lang, summary "
            "FROM articles "
            "WHERE validated = 0 AND archived = 0 "
            "ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


async def list_unvalidated(limit: int = 20) -> list[dict]:
    return await asyncio.to_thread(_list_unvalidated_sync, limit)


def _set_article_validated_sync(
    url: str, status: int, reason: str | None = None,
) -> bool:
    """status: 1=pass, -1=fail, 0=reset to pending."""
    now = int(time.time())
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE articles "
            "SET validated = ?, validation_reason = ?, validated_at = ? "
            "WHERE url = ?",
            (status, reason, now, url),
        )
        return cur.rowcount > 0


async def set_article_validated(
    url: str, status: int, reason: str | None = None,
) -> bool:
    return await asyncio.to_thread(_set_article_validated_sync, url, status, reason)


def _validation_stats_sync() -> dict:
    """Snapshot of the validation queue for the lab dashboard."""
    with closing(_conn()) as c:
        c1 = c.execute("SELECT COUNT(*) AS n FROM articles WHERE validated = 0").fetchone()
        c2 = c.execute("SELECT COUNT(*) AS n FROM articles WHERE validated = 1").fetchone()
        c3 = c.execute("SELECT COUNT(*) AS n FROM articles WHERE validated = -1").fetchone()
        # Failure breakdown — surface the top reasons so we can tighten
        # outlet config when a particular failure dominates.
        reason_rows = c.execute(
            "SELECT validation_reason AS reason, COUNT(*) AS n "
            "FROM articles WHERE validated = -1 AND validation_reason IS NOT NULL "
            "GROUP BY validation_reason ORDER BY n DESC LIMIT 12"
        ).fetchall()
        recent_rows = c.execute(
            "SELECT url, outlet, validated, validation_reason, validated_at "
            "FROM articles WHERE validated_at IS NOT NULL "
            "ORDER BY validated_at DESC LIMIT 15"
        ).fetchall()
        return {
            "pending": c1["n"],
            "validated": c2["n"],
            "failed": c3["n"],
            "reasons": [dict(r) for r in reason_rows],
            "recent": [dict(r) for r in recent_rows],
        }


async def validation_stats() -> dict:
    return await asyncio.to_thread(_validation_stats_sync)


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


def _reader_urls_present_sync(urls: list[str]) -> set[str]:
    """For the body-first feed gate: return the subset of `urls` that
    already have a row in reader_results — i.e. trafilatura extraction
    has succeeded at least once and the cleaned body is on disk. URLs
    without a row are dropped from /api/brief so users never click into
    a "could not extract the article body" error.
    Skips empty/whitespace payloads so a stub reader row doesn't pass."""
    if not urls:
        return set()
    out: set[str] = set()
    with closing(_conn()) as c:
        for i in range(0, len(urls), 200):
            chunk = urls[i:i + 200]
            placeholders = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT url, payload_json FROM reader_results "
                f"WHERE url IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                payload = r["payload_json"] or ""
                if not payload.strip() or payload.strip() in ("{}", "null"):
                    continue
                out.add(r["url"])
    return out


async def reader_urls_present(urls: list[str]) -> set[str]:
    return await asyncio.to_thread(_reader_urls_present_sync, urls)


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


# ── extraction health ─────────────────────────────────────────────
# Trimmed to the most recent EXTRACT_LOG_KEEP rows on write so the table
# can't grow without bound on a 1 GB box. That's ~2 weeks of traffic at
# current volume, which is all the health view looks at anyway.
EXTRACT_LOG_KEEP = 5000


def _record_extract_result_sync(outlet: str, url: str, ok: bool, reason: str) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO extract_log(ts, outlet, url, ok, reason) VALUES(?,?,?,?,?)",
            (int(time.time()), outlet or "", url, 1 if ok else 0, reason or ""),
        )
        # Cheap trim: only bother when we're plausibly over the cap.
        n = c.execute("SELECT COUNT(*) AS n FROM extract_log").fetchone()["n"]
        if n > EXTRACT_LOG_KEEP * 1.2:
            c.execute(
                "DELETE FROM extract_log WHERE id NOT IN ("
                "  SELECT id FROM extract_log ORDER BY id DESC LIMIT ?)",
                (EXTRACT_LOG_KEEP,),
            )


async def record_extract_result(outlet: str, url: str, ok: bool, reason: str) -> None:
    """Log one reader extraction attempt. Never raises — health telemetry
    must not be able to break article rendering."""
    try:
        await asyncio.to_thread(_record_extract_result_sync, outlet, url, ok, reason)
    except Exception as exc:      # pragma: no cover - telemetry only
        print(f"[db] record_extract_result failed: {exc!r}", flush=True)


def _extract_health_sync(hours: int) -> dict:
    since = int(time.time()) - hours * 3600
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT outlet,"
            "       COUNT(*) AS attempts,"
            "       SUM(ok) AS ok,"
            "       MAX(CASE WHEN ok=1 THEN ts END) AS last_ok,"
            "       MAX(ts) AS last_try "
            "FROM extract_log WHERE ts > ? GROUP BY outlet ORDER BY attempts DESC",
            (since,),
        ).fetchall()
        reasons = c.execute(
            "SELECT reason, COUNT(*) AS n FROM extract_log "
            "WHERE ts > ? AND ok = 0 AND reason <> '' "
            "GROUP BY reason ORDER BY n DESC",
            (since,),
        ).fetchall()

    outlets = []
    tot_att = tot_ok = 0
    for r in rows:
        attempts = r["attempts"] or 0
        ok = r["ok"] or 0
        tot_att += attempts
        tot_ok += ok
        outlets.append({
            "outlet": r["outlet"] or "(unknown)",
            "attempts": attempts,
            "ok": ok,
            "failed": attempts - ok,
            "success_rate": round(ok / attempts, 3) if attempts else 0.0,
            "last_ok": r["last_ok"],
            "last_try": r["last_try"],
        })
    return {
        "window_hours": hours,
        "attempts": tot_att,
        "ok": tot_ok,
        "failed": tot_att - tot_ok,
        "success_rate": round(tot_ok / tot_att, 3) if tot_att else None,
        "outlets": outlets,
        "reasons": {r["reason"]: r["n"] for r in reasons},
    }


async def extract_health(hours: int = 24) -> dict:
    """Per-outlet reader success rate over the last `hours`."""
    return await asyncio.to_thread(_extract_health_sync, hours)


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


# ── Maintenance ────────────────────────────────────────────────────
# Pruning + VACUUM keeps the DB bounded without us hand-holding it.
# Lab "STORAGE & MEMORY" card surfaces whatever we trimmed so the user
# can see the agent doing its job.
ARTICLE_RETENTION_DAYS = 60       # articles older than this get dropped
READER_CACHE_LIMIT = 6000         # keep newest N reader_results rows
AGENT_RUNS_LIMIT = 500            # already trimmed inside log_agent_run


def _prune_sync() -> dict:
    """One-shot prune. Returns counts of what was removed + final size."""
    now = int(time.time())
    cutoff = now - ARTICLE_RETENTION_DAYS * 86_400
    removed_articles = 0
    removed_reader = 0
    with closing(_conn()) as c:
        # 1. Drop articles older than retention. Keep any that are
        #    referenced by a recent reader_results last_used_at — if a
        #    user opened it recently we keep the metadata around.
        cur = c.execute(
            "DELETE FROM articles WHERE fetched_at < ? AND "
            "url NOT IN (SELECT url FROM reader_results WHERE last_used_at > ?)",
            (cutoff, cutoff),
        )
        removed_articles = cur.rowcount or 0
        # 2. Cap reader_results at READER_CACHE_LIMIT, keeping the most
        #    recently USED rows (cross-user popularity wins).
        total = c.execute("SELECT COUNT(*) AS n FROM reader_results").fetchone()["n"]
        if total > READER_CACHE_LIMIT:
            excess = total - READER_CACHE_LIMIT
            cur = c.execute(
                "DELETE FROM reader_results WHERE url IN ("
                "  SELECT url FROM reader_results ORDER BY last_used_at ASC LIMIT ?"
                ")",
                (excess,),
            )
            removed_reader = cur.rowcount or 0
        # 3. VACUUM reclaims pages from deletes — actually shrinks the
        #    file on disk. Cheap on a 20-200MB DB; takes a few seconds.
        c.execute("VACUUM")
    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {
        "removed_articles": removed_articles,
        "removed_reader": removed_reader,
        "db_bytes_after": db_bytes,
        "ran_at": now,
    }


async def prune_once() -> dict:
    return await asyncio.to_thread(_prune_sync)


# ── API call ledger ────────────────────────────────────────────────
# Every LLM round-trip lands here so the lab can show the user
# exactly when / how often / how expensively the agents talk to the
# paid APIs. Critical for cost monitoring on the Claude side.
#
# Per-1M-token prices are baked in here; if Anthropic/Google change
# them, update _COST_TABLE below.
_COST_TABLE = {
    # provider key: (input_per_M_usd, output_per_M_usd)
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-5":   (15.00, 75.00),
    # Gemini 2.5 Flash free-tier on AI Studio = $0 up to limit.
    # Paid tier prices kept here in case the user switches.
    "gemini-2.5-flash":  (0.30, 2.50),
    "gemini-2.0-flash":  (0.075, 0.30),
}


def _estimate_cost(provider: str, in_tok: int, out_tok: int) -> float:
    p = _COST_TABLE.get(provider, (0.0, 0.0))
    return (in_tok * p[0] + out_tok * p[1]) / 1_000_000


def _log_api_call_sync(
    provider: str, purpose: str,
    input_tokens: int, output_tokens: int,
    success: bool, note: str | None,
) -> None:
    now = int(time.time())
    cost = _estimate_cost(provider, input_tokens, output_tokens)
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO api_calls "
            "(ts, provider, purpose, input_tokens, output_tokens, cost_usd, success, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, provider, purpose, input_tokens, output_tokens, cost,
             1 if success else 0, (note or "")[:200]),
        )


async def log_api_call(
    provider: str, purpose: str, *,
    input_tokens: int = 0, output_tokens: int = 0,
    success: bool = True, note: str | None = None,
) -> None:
    """Fire-and-forget logger. Never raises — cost telemetry must
    never break the actual API path that's calling us."""
    try:
        await asyncio.to_thread(
            _log_api_call_sync,
            provider, purpose, input_tokens, output_tokens, success, note,
        )
    except Exception as exc:
        print(f"[api_log] write failed: {exc!r}", flush=True)


def _api_usage_sync() -> dict:
    """Aggregate views for the lab USAGE card."""
    now = int(time.time())
    day_ago = now - 86_400
    week_ago = now - 7 * 86_400
    with closing(_conn()) as c:
        def _slice(where: str, args: list) -> dict:
            r = c.execute(
                f"SELECT COUNT(*) AS calls, "
                f"COALESCE(SUM(input_tokens), 0) AS in_tok, "
                f"COALESCE(SUM(output_tokens), 0) AS out_tok, "
                f"COALESCE(SUM(cost_usd), 0) AS cost "
                f"FROM api_calls WHERE {where}",
                args,
            ).fetchone()
            return dict(r)
        today = _slice("ts > ?", [day_ago])
        week  = _slice("ts > ?", [week_ago])
        total = _slice("1=1", [])
        # Per-provider today
        prov_rows = c.execute(
            "SELECT provider, COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd), 0) AS cost, "
            "COALESCE(SUM(input_tokens), 0) AS in_tok, "
            "COALESCE(SUM(output_tokens), 0) AS out_tok "
            "FROM api_calls WHERE ts > ? GROUP BY provider "
            "ORDER BY cost DESC",
            [day_ago],
        ).fetchall()
        # Per-purpose today
        purpose_rows = c.execute(
            "SELECT purpose, COUNT(*) AS calls, "
            "COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM api_calls WHERE ts > ? GROUP BY purpose "
            "ORDER BY cost DESC",
            [day_ago],
        ).fetchall()
        # Recent log lines
        recent = c.execute(
            "SELECT ts, provider, purpose, input_tokens, output_tokens, "
            "cost_usd, success, note "
            "FROM api_calls ORDER BY ts DESC LIMIT 20"
        ).fetchall()
        return {
            "today": today,
            "week": week,
            "total": total,
            "by_provider_today": [dict(r) for r in prov_rows],
            "by_purpose_today":  [dict(r) for r in purpose_rows],
            "recent": [dict(r) for r in recent],
        }


async def api_usage() -> dict:
    return await asyncio.to_thread(_api_usage_sync)

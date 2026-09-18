"""The archivist department — writes every displayed article to disk.

Why this exists
───────────────
Everything the app knows lives in one SQLite file. That file is the
single point of failure for the whole corpus: a publisher deleting a
story, a paywall going up, a prune running, or the file itself getting
corrupted all end the same way — the article is gone and nothing can
bring it back. `reader_results` protects the body only until the prune
caps that table at 6000 rows.

So: as articles are served to the page, they are also written out as
plain files under `archive/`. No database, no proprietary format — one
JSON object per line, grouped by date. `grep`, `jq`, and a text editor
all work on it, and it survives the DB being deleted outright.

Where it sits in the sequence
─────────────────────────────
    display  →  record_displayed()      (in-memory, no I/O)
                       ↓
    archivist worker   →  flush()       (batched file write)
                       ↓
              db.mark_archived_to_disk()
                       ↓
    prune  →  deletes ONLY stamped rows

The hook on the display path does nothing but add strings to a set. All
the I/O happens in the worker, off the request path, because this runs
on a 416MB Lightsail box where /api/brief latency is the thing that
actually gets noticed.

The stamp is written AFTER the file write returns. A crash mid-write
therefore leaves the row unstamped and it is archived again on the next
pass — a duplicate line in a file, which the reader deduplicates. The
opposite ordering would lose articles, so the ordering is not an
accident.

Layout
──────
    archive/2026/09/2026-09-18.ndjson     one line per article
    archive/README.md                     what this is, how to read it

Partitioned by publication date so a day's news is one file, and a
file stays a size a human can actually open.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# archive/ sits beside backend/ and frontend/ at the repo root, so the
# corpus is visible next to the code that produced it rather than buried
# in backend/data/ with the SQLite file it is meant to outlive.
ARCHIVE_DIR = Path(__file__).resolve().parent.parent.parent / "archive"

# Articles seen on the display path, waiting to be written. A set, so a
# story that stays on the front page for six hours costs one write, not
# one per request.
_pending: set[str] = set()
_lock = threading.Lock()

# Cap on the pending set. If the worker dies, this stops an unbounded
# set from eating the box's memory — the DB backlog scan picks up
# anything dropped here, so nothing is actually lost by capping.
_PENDING_CAP = 5000

_stats = {"queued": 0, "written": 0, "files": 0, "last_write": 0, "dropped": 0}


def record_displayed(items: Iterable[Any]) -> None:
    """Note that these articles were shown. Called from the display path.

    Must stay cheap and must never raise: it runs inside the request
    path of the app's busiest endpoint. Accepts dicts (feed items) or
    bare URL strings.
    """
    try:
        urls = []
        for it in items:
            if isinstance(it, dict):
                u = it.get("url")
            else:
                u = it
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
        if not urls:
            return
        with _lock:
            room = _PENDING_CAP - len(_pending)
            if room <= 0:
                _stats["dropped"] += len(urls)
                return
            before = len(_pending)
            _pending.update(urls[:room])
            _stats["queued"] += len(_pending) - before
    except Exception:
        # A failure to archive must never cost the user their page.
        pass


def take_pending(limit: int = 500) -> list[str]:
    """Remove and return up to `limit` queued URLs."""
    with _lock:
        batch = list(_pending)[:limit]
        _pending.difference_update(batch)
        return batch


def pending_count() -> int:
    with _lock:
        return len(_pending)


def _day_file(row: dict) -> Path:
    """Which file this article belongs in.

    Publication date where we have it, fetch date otherwise — the point
    is that a day's news lands together, and published_at is the honest
    answer to "when was this news".
    """
    ts = row.get("published_ts") or row.get("fetched_at") or time.time()
    try:
        d = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
    except (ValueError, OSError, OverflowError):
        d = _dt.datetime.now(tz=_dt.timezone.utc)
    return ARCHIVE_DIR / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.ndjson"


def _record(row: dict) -> dict:
    """Shape one article for the archive.

    The body is unwrapped out of reader_results' payload_json and stored
    as a plain list of paragraphs, so a reader of this file never needs
    to know how this app happened to cache things.
    """
    body_text: list[str] = []
    body_meta: dict = {}
    raw = row.get("body_json")
    if raw:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(payload, dict):
                paras = payload.get("paragraphs") or payload.get("body") or []
                if isinstance(paras, list):
                    body_text = [p for p in paras if isinstance(p, str)]
                for k in ("title", "byline", "published", "site", "image"):
                    v = payload.get(k)
                    if v:
                        body_meta[k] = v
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "url": row.get("url"),
        "title": row.get("title"),
        "title_ko": row.get("title_ko"),
        "outlet": row.get("outlet"),
        "category": row.get("category"),
        "lang": row.get("lang"),
        "summary": row.get("summary"),
        "dek_ko": row.get("dek_ko"),
        "why": row.get("why"),
        "image": row.get("image"),
        "score": row.get("score"),
        "premium": row.get("premium"),
        "corroboration": row.get("corroboration"),
        "published_at": row.get("published_at"),
        "published_ts": row.get("published_ts"),
        "fetched_at": row.get("fetched_at"),
        "body": body_text,
        "body_meta": body_meta or None,
        "has_body": bool(body_text),
        "archived_at": int(time.time()),
    }


def _existing_urls(path: Path) -> set[str]:
    """URLs already in this day's file, so re-archiving doesn't duplicate.

    A corrupt line (half-written during a power loss) is skipped rather
    than fatal — one unreadable line must not make the whole day
    unappendable.
    """
    if not path.exists():
        return set()
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    u = json.loads(line).get("url")
                except json.JSONDecodeError:
                    continue
                if u:
                    seen.add(u)
    except OSError:
        return set()
    return seen


def _write_day(path: Path, records: list[dict]) -> int:
    """Append records to one day-file, skipping ones already there.

    Append rather than rewrite: an interrupted append costs at most one
    malformed trailing line, whereas an interrupted rewrite costs the
    whole day. fsync because the point of this file is to survive the
    machine dying.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    have = _existing_urls(path)
    fresh = [r for r in records if r.get("url") and r["url"] not in have]
    if not fresh:
        return 0

    # Heal the boundary before appending. A previous append that died
    # mid-line leaves the file without a trailing newline, and appending
    # onto that glues the next record to the broken one — producing a
    # single unparseable line and silently swallowing a whole article
    # that we then report as durable. Caught in testing: the record was
    # stamped archived_to_disk_at while being unreadable on disk, which
    # is exactly the state the prune guard must never trust.
    #
    # One newline turns the truncated fragment into an isolated bad line
    # (which the reader already skips) and keeps the new record intact.
    needs_nl = False
    try:
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_nl = fh.read(1) != b"\n"
    except OSError:
        needs_nl = False

    with path.open("a", encoding="utf-8") as fh:
        if needs_nl:
            fh.write("\n")
        for r in fresh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return len(fresh)


def _write_sync(rows: list[dict]) -> tuple[int, list[str]]:
    """Group rows by day and append. Returns (written, urls_safely_on_disk).

    A day that fails to write leaves its URLs out of the returned list,
    so they stay unstamped and get retried — rather than being marked
    safe and then pruned.
    """
    by_day: dict[Path, list[dict]] = {}
    for row in rows:
        by_day.setdefault(_day_file(row), []).append(_record(row))

    written = 0
    safe: list[str] = []
    for path, recs in by_day.items():
        try:
            written += _write_day(path, recs)
        except OSError as exc:
            print(f"[archivist] write failed {path.name}: {exc!r}", flush=True)
            continue
        # Everything routed to a file that wrote cleanly is durable now,
        # including records skipped as already-present.
        safe.extend(r["url"] for r in recs if r.get("url"))
    return written, safe


async def flush(rows: list[dict]) -> tuple[int, list[str]]:
    """Write these article rows to the archive. Returns (written, safe_urls)."""
    if not rows:
        return 0, []
    written, safe = await asyncio.to_thread(_write_sync, rows)
    _stats["written"] += written
    _stats["last_write"] = int(time.time())
    return written, safe


def stats() -> dict:
    out = dict(_stats)
    out["pending"] = pending_count()
    out["dir"] = str(ARCHIVE_DIR)
    try:
        files = list(ARCHIVE_DIR.rglob("*.ndjson"))
        out["files"] = len(files)
        out["bytes"] = sum(f.stat().st_size for f in files)
    except OSError:
        pass
    return out

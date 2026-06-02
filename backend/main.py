"""FastAPI app: serves API + static frontend."""

from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import auth, cache, config, db, mixer, tickers
from backend.agent import (
    curator, dedup, illustrator, reader, registry, sorter, summary,
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
    try:
        asyncio.create_task(_refresh_agent())
    except Exception as exc:
        print(f"[startup] refresh agent failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_validation_worker())
    except Exception as exc:
        print(f"[startup] validation worker failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_prune_worker())
    except Exception as exc:
        print(f"[startup] prune worker failed to schedule: {exc!r}", flush=True)
    try:
        asyncio.create_task(_resummary_worker())
    except Exception as exc:
        print(f"[startup] resummary worker failed to schedule: {exc!r}", flush=True)


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
_INGEST_BATCH = 6           # how many articles per cycle
_INGEST_PAUSE_BUSY = 2.0    # seconds between batches while there's work
_INGEST_PAUSE_IDLE = 30.0   # seconds between polls when queue is empty


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
            return -1, f"extract-error:{type(exc).__name__}"
        if not reading or not (reading.text or reading.excerpt):
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

    # Quality gate: drop anything the validation worker has already
    # marked as failing (validated=-1) so the feed never shows articles
    # that can't actually be opened. Pending (validated=0) and passing
    # (validated=1) items still flow through; the worker chips through
    # the backlog within minutes after a fresh deploy.
    try:
        top_urls = [t.get("url") for t in top if t.get("url")]
        states = await db.get_validation_states(top_urls)
        rejected = {u for u, v in states.items() if v == -1}
        if rejected:
            top = [t for t in top if t.get("url") not in rejected]
            print(f"[gate] dropped {len(rejected)} validated=-1 items", flush=True)
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

    # Trim the wire payload: only ship the first ~60 mixed items (enough
    # for the initial 2 pages of 31). The rest of the archive is reached
    # via /api/page on demand, which reads straight from SQLite. This
    # keeps initial load tiny — was ~750 KB, now ~150 KB.
    INITIAL_WIRE_ITEMS = 62
    mixed_wire = mixed[:INITIAL_WIRE_ITEMS]

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
    # Longer cache → far fewer curator + summary LLM round-trips. The
    # user spent ~$20/day on Anthropic before this knob existed.
    cache.set("brief:response", payload, 120)

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
        payload["db_total_articles"] = await db.count_articles()
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

    full_key = f"article_reader:{url}"
    cached = cache.get(full_key)
    if cached is not None:
        await db.bump_counter("reader_cache_hits_mem")
        cached = _scrub_cached_paragraphs(cached)
        if fallback_title and _is_bad_title(cached.get("title") or ""):
            cached = {**cached, "title": fallback_title}
        return cached

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
        return saved

    reading = await reader.extract(url)
    if not reading:
        return {"error": "could not extract the article body"}

    # No LLM rewrite — show the publisher's body directly, just cleaned
    # of emojis / section headers / photo credits / mojibake. Cheaper,
    # faster, and the user explicitly asked for the original article
    # ("괜히 에이아이로 돌리지말고 그냥 바로 기사 보여줘").
    raw_title = html.unescape(reading.title or "")
    clean_title = _better_title(raw_title, fallback_title or "")
    paragraphs = _dedup_with_title(_split_paragraphs(reading.text), clean_title)
    result = {
        "url": url,
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
    return result


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


@app.get("/api/page")
async def api_page(
    n: int = 1, size: int = 31,
    cat: Optional[str] = None, premium: int = 0,
):
    """Lazy pagination — page 2+ pulls from the SQLite archive instead
    of the in-memory curator output. Total page count grows as the DB
    grows, with no upper limit baked in. Pass `premium=1` to filter to
    only premium-outlet articles."""
    n = max(1, n)
    size = max(1, min(size, 100))
    cat_clean = cat if cat and cat != "all" else None
    premium_only = bool(premium)
    total = await db.count_articles(cat=cat_clean)
    pages = max(1, (total + size - 1) // size)
    n = min(n, pages)
    items = await db.list_articles(
        offset=(n - 1) * size,
        limit=size,
        cat=cat_clean,
        premium_only=premium_only,
    )
    converted = [
        {
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
            # Card-level translation — populated on /api/translate
            # success. Presence drives the neon border + ✦한 badge.
            "title_ko": r.get("title_ko"),
            "dek_ko": r.get("dek_ko"),
            "translated_at": r.get("translated_at"),
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

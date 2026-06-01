"""Tiny session-cookie auth for dailybrief.

Why this exists:
  - The translate endpoint costs money on cache miss.  Free-tier
    Gemini is generous but not unlimited, and Claude isn't free.
  - So we gate /api/translate behind "logged-in user with an active
    subscription". Open-web visitors browse the feed exactly as before;
    they just don't see the translate button.

Implementation:
  - sqlite-backed `users` + `sessions` tables (added in db.py).
  - PBKDF2-HMAC-SHA256 for password hashing — stdlib only, no new dep.
  - Session token = secrets.token_urlsafe(32). Stored in an HttpOnly
    cookie + the sessions table. Default lifetime: 30 days.
  - FastAPI dependency `require_subscriber(request)` raises 403 unless
    the cookie maps to a user with subscription=1.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException, Request, Response


COOKIE_NAME = "db_session"
SESSION_TTL = 30 * 86_400      # 30 days


# Re-use db.py's connection so the same DB file is touched.
def _conn() -> sqlite3.Connection:
    from backend.db import _conn as _real_conn
    return _real_conn()


# ── Schema ──────────────────────────────────────────────────────────
def _init_sync() -> None:
    with closing(_conn()) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                subscription    INTEGER NOT NULL DEFAULT 0,
                is_admin        INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token         TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                expires_at    INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        """)


async def init() -> None:
    await asyncio.to_thread(_init_sync)
    # Seed admin/admin on first boot if there are no users yet.
    await _seed_admin_if_empty()


# ── Password hashing (pbkdf2_hmac sha256) ──────────────────────────
def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256$120000${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    got = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iters),
    )
    return hmac.compare_digest(expected, got)


# ── Users ──────────────────────────────────────────────────────────
def _create_user_sync(
    username: str, password: str, *,
    subscription: bool = False, is_admin: bool = False,
) -> int | None:
    now = int(time.time())
    ph = _hash_password(password)
    with closing(_conn()) as c:
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash, subscription, is_admin, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, ph, int(subscription), int(is_admin), now),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


async def create_user(
    username: str, password: str, *,
    subscription: bool = False, is_admin: bool = False,
) -> int | None:
    return await asyncio.to_thread(
        _create_user_sync, username, password,
        subscription=subscription, is_admin=is_admin,
    )


def _get_user_by_username_sync(username: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT id, username, password_hash, subscription, is_admin, created_at "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def _get_user_by_id_sync(user_id: int) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT id, username, subscription, is_admin, created_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


async def _seed_admin_if_empty() -> None:
    def _go() -> None:
        with closing(_conn()) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            if n > 0:
                return
        # No users — seed admin/admin with subscription + admin flag.
        # First-login should rotate this password via /api/auth/change-password.
        _create_user_sync(
            "admin", "admin", subscription=True, is_admin=True,
        )
        print("[auth] seeded admin/admin (CHANGE THIS PASSWORD)", flush=True)
    await asyncio.to_thread(_go)


# ── Sessions ───────────────────────────────────────────────────────
def _create_session_sync(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    exp = now + SESSION_TTL
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, exp, now),
        )
    return token


async def create_session(user_id: int) -> str:
    return await asyncio.to_thread(_create_session_sync, user_id)


def _lookup_session_sync(token: str) -> dict | None:
    now = int(time.time())
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT s.user_id, s.expires_at, u.username, u.subscription, u.is_admin "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, now),
        ).fetchone()
        return dict(row) if row else None


def _delete_session_sync(token: str) -> None:
    with closing(_conn()) as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


async def delete_session(token: str) -> None:
    await asyncio.to_thread(_delete_session_sync, token)


# ── FastAPI helpers ────────────────────────────────────────────────
async def current_user(request: Request) -> dict | None:
    """Return the current user dict (or None if anonymous). Cheap —
    one indexed SQL hit per request."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return await asyncio.to_thread(_lookup_session_sync, token)


async def require_subscriber(request: Request) -> dict:
    """Dependency for endpoints that need an active subscription.
    Raises 401 if not logged in, 403 if logged in without subscription."""
    user = await current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if not user.get("subscription"):
        raise HTTPException(status_code=403, detail="subscription required")
    return user


# ── Cookie helpers ─────────────────────────────────────────────────
def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        # Secure cookie when served over https. Behind Caddy on
        # fluentfun.org we always serve TLS; locally we'd want
        # secure=False to test, but the cookie still works either way.
        secure=os.getenv("DAILYBRIEF_SECURE_COOKIE", "0") == "1",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")

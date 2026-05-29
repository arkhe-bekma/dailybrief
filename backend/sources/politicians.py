"""Politician / insider stock trade feed.

Real source: House Stock Watcher public dataset (no API key).
  https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json

Falls back to mock data if the fetch fails.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

import httpx

from backend import cache


HSW_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"


@dataclass
class InsiderTrade:
    timestamp: str
    name: str
    role: str
    action: str          # "BUY" | "SELL"
    ticker: str
    company: str
    size_band: str
    source_url: str

    def to_dict(self) -> dict:
        return asdict(self)


# Used when remote fetch fails.
_MOCK = [
    ("Nancy Pelosi",     "Rep. (D-CA)",  "BUY",  "NVDA", "Nvidia",      "$1M–$5M"),
    ("Tommy Tuberville", "Sen. (R-AL)",  "BUY",  "MSFT", "Microsoft",   "$50K–$100K"),
    ("Donald Trump Jr.", "Private",      "BUY",  "DJT",  "Trump Media", "$250K–$500K"),
    ("Dan Crenshaw",     "Rep. (R-TX)",  "SELL", "TSLA", "Tesla",       "$15K–$50K"),
    ("Ro Khanna",        "Rep. (D-CA)",  "BUY",  "AMD",  "AMD",         "$15K–$50K"),
    ("Mark Green",       "Rep. (R-TN)",  "BUY",  "AVGO", "Broadcom",    "$100K–$250K"),
]


def _normalize_action(t: dict) -> str:
    raw = (t.get("type") or "").lower()
    if "purchase" in raw or "buy" in raw:
        return "BUY"
    if "sale" in raw or "sell" in raw:
        return "SELL"
    return "BUY"


def _normalize_band(t: dict) -> str:
    raw = (t.get("amount") or "").strip()
    # House discloses bands like "$1,001 - $15,000"
    if not raw:
        return "—"
    # Trim and shorten thousands.
    short = raw.replace(",", "").replace("$", "")
    parts = [p.strip() for p in short.split("-")]
    if len(parts) == 2:
        try:
            lo = int(parts[0]); hi = int(parts[1])
            def fmt(n: int) -> str:
                if n >= 1_000_000: return f"${n // 1_000_000}M"
                if n >= 1000:      return f"${n // 1000}K"
                return f"${n}"
            return f"{fmt(lo)}–{fmt(hi)}"
        except ValueError:
            pass
    return raw


async def _fetch_real() -> list[InsiderTrade] | None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(HSW_URL, timeout=15.0)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        print(f"[politicians] real fetch failed: {exc}")
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    trades: list[tuple[datetime, InsiderTrade]] = []
    for t in data:
        try:
            dt = datetime.fromisoformat(t.get("transaction_date", "")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < cutoff:
            continue
        ticker = (t.get("ticker") or "").upper().strip()
        if not ticker or ticker in {"--", "N/A"}:
            continue
        trades.append((dt, InsiderTrade(
            timestamp=dt.isoformat(),
            name=t.get("representative", "Unknown"),
            role="Rep.",
            action=_normalize_action(t),
            ticker=ticker,
            company=t.get("asset_description", "")[:60],
            size_band=_normalize_band(t),
            source_url="https://housestockwatcher.com/",
        )))

    trades.sort(key=lambda p: p[0], reverse=True)
    return [t for _, t in trades[:30]]


async def fetch() -> list[InsiderTrade]:
    cached = cache.get("trades:list")
    if cached is not None:
        return cached
    real = await _fetch_real()
    if real:
        cache.set("trades:list", real, 3600)
        return real

    # Fallback: mock
    now = datetime.now(timezone.utc)
    mocked = [
        InsiderTrade(
            timestamp=(now - timedelta(hours=i * 3)).isoformat(),
            name=name, role=role, action=action,
            ticker=ticker, company=company, size_band=band,
            source_url="https://www.capitoltrades.com/",
        )
        for i, (name, role, action, ticker, company, band) in enumerate(_MOCK)
    ]
    cache.set("trades:list", mocked, 600)
    return mocked

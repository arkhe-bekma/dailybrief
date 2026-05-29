"""Whale movement feed.

Real source: Whale Alert API. Needs WHALE_ALERT_API_KEY env var.
  https://docs.whale-alert.io/

Falls back to mock data if no key / fetch fails.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

import httpx

from backend import cache


@dataclass
class WhaleMove:
    timestamp: str
    asset: str
    amount_usd: float
    from_label: str
    to_label: str
    tx_url: str

    def to_dict(self) -> dict:
        return asdict(self)


async def _fetch_real() -> list[WhaleMove] | None:
    key = os.getenv("WHALE_ALERT_API_KEY")
    if not key:
        return None
    start = int((datetime.now(timezone.utc) - timedelta(hours=6)).timestamp())
    url = (
        "https://api.whale-alert.io/v1/transactions"
        f"?api_key={key}&min_value=1000000&start={start}&limit=20"
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10.0)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        print(f"[whales] real fetch failed: {exc}")
        return None

    moves: list[WhaleMove] = []
    for tx in data.get("transactions", []):
        ts = datetime.fromtimestamp(tx.get("timestamp", 0), tz=timezone.utc).isoformat()
        moves.append(WhaleMove(
            timestamp=ts,
            asset=tx.get("symbol", "?").upper(),
            amount_usd=float(tx.get("amount_usd", 0)),
            from_label=(tx.get("from", {}) or {}).get("owner") or "Unknown",
            to_label=(tx.get("to", {}) or {}).get("owner") or "Unknown",
            tx_url=f"https://whale-alert.io/transaction/{tx.get('blockchain', '')}/{tx.get('hash', '')}",
        ))
    return moves


_MOCK_TEMPLATES = [
    ("BTC",  "Unknown wallet",  "Binance"),
    ("ETH",  "Coinbase",        "Unknown wallet"),
    ("USDT", "Tether Treasury", "Binance"),
    ("BTC",  "Kraken",          "Unknown wallet"),
    ("ETH",  "Unknown wallet",  "Coinbase"),
    ("SOL",  "Unknown wallet",  "Binance"),
]


async def fetch() -> list[WhaleMove]:
    cached = cache.get("whales:list")
    if cached is not None:
        return cached

    real = await _fetch_real()
    if real:
        cache.set("whales:list", real, 120)
        return real

    now = datetime.now(timezone.utc)
    moves = [
        WhaleMove(
            timestamp=(now - timedelta(minutes=i * 17)).isoformat(),
            asset=asset,
            amount_usd=round(random.uniform(5_000_000, 120_000_000), 0),
            from_label=src,
            to_label=dst,
            tx_url="https://whale-alert.io/",
        )
        for i, (asset, src, dst) in enumerate(_MOCK_TEMPLATES)
    ]
    cache.set("whales:list", moves, 300)
    return moves

"""Price feed for the ticker tape + sparklines.

- Stocks / indices / FX: Stooq batch CSV (one HTTP call, no auth, no rate
  limits in normal use).
- Crypto: CoinGecko free markets endpoint.
- Sparklines for news cards: best-effort via Yahoo's batch `spark` endpoint;
  gracefully empty when Yahoo throttles.
"""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass, asdict

import httpx

from backend import cache


# label, stooq symbol, coingecko id (for crypto). Use exactly one of stooq/coingecko.
TAPE = [
    {"label": "NVDA",     "stooq": "nvda.us"},
    {"label": "AMD",      "stooq": "amd.us"},
    {"label": "TSMC",     "stooq": "tsm.us"},
    {"label": "MSFT",     "stooq": "msft.us"},
    {"label": "GOOGL",    "stooq": "googl.us"},
    {"label": "TSLA",     "stooq": "tsla.us"},
    {"label": "S&P 500",  "stooq": "^spx"},
    {"label": "NASDAQ",   "stooq": "^ndq"},
    {"label": "KOSPI",    "stooq": "^kospi"},
    {"label": "USD/KRW",  "stooq": "usdkrw"},
    {"label": "BTC",      "coingecko": "bitcoin"},
    {"label": "ETH",      "coingecko": "ethereum"},
    {"label": "SOL",      "coingecko": "solana"},
]


@dataclass
class Quote:
    symbol: str
    label: str
    price: float
    change_pct: float
    spark: list[float] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/csv,application/json,*/*",
}


# ── Stooq batch (price + change %) ─────────────────────────────────
async def _stooq_quotes(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, tuple[float, float]]:
    if not symbols:
        return {}
    url = (
        "https://stooq.com/q/l/?s="
        + "+".join(symbols)
        + "&f=sd2t2ohlcvp&h&e=csv"   # 'p' adds Prev Close column
    )
    try:
        r = await client.get(url, headers=_BROWSER, timeout=10.0)
        r.raise_for_status()
        text = r.text
    except Exception as exc:
        print(f"[prices] stooq failed: {exc}")
        return {}

    out: dict[str, tuple[float, float]] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sym = (row.get("Symbol") or "").lower()
        close_raw = row.get("Close")
        prev_raw = row.get("Prev")
        if not sym or close_raw in (None, "N/D"):
            continue
        try:
            close = float(close_raw)
            prev = float(prev_raw) if prev_raw not in (None, "N/D") else close
            change = ((close - prev) / prev * 100) if prev else 0.0
            out[sym] = (close, change)
        except (TypeError, ValueError):
            continue
    return out


# ── CoinGecko crypto ───────────────────────────────────────────────
async def _coingecko_quotes(client: httpx.AsyncClient, ids: list[str]) -> dict[str, tuple[float, float, list[float]]]:
    if not ids:
        return {}
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={','.join(ids)}"
        "&price_change_percentage=24h&sparkline=true"
    )
    try:
        r = await client.get(url, timeout=10.0)
        r.raise_for_status()
        out: dict[str, tuple[float, float, list[float]]] = {}
        for row in r.json():
            cid = row.get("id")
            if not cid:
                continue
            spark = (row.get("sparkline_in_7d") or {}).get("price") or []
            if len(spark) > 40:
                step = len(spark) // 40
                spark = spark[::step][-40:]
            out[cid] = (
                float(row.get("current_price") or 0),
                float(row.get("price_change_percentage_24h_in_currency") or 0),
                spark,
            )
        return out
    except Exception as exc:
        print(f"[prices] coingecko failed: {exc}")
        return {}


# ── Yahoo best-effort spark batch (for news card sparklines) ───────
async def _yahoo_sparks(client: httpx.AsyncClient, symbols: list[str]) -> dict[str, list[float]]:
    if not symbols:
        return {}
    url = (
        "https://query2.finance.yahoo.com/v7/finance/spark"
        f"?symbols={','.join(symbols)}&range=5d&interval=60m"
    )
    try:
        r = await client.get(url, headers=_BROWSER, timeout=10.0)
        r.raise_for_status()
        results = r.json().get("spark", {}).get("result", []) or []
    except Exception as exc:
        print(f"[prices] yahoo sparks failed: {exc}")
        return {}

    out: dict[str, list[float]] = {}
    for entry in results:
        sym = entry.get("symbol")
        resp = (entry.get("response") or [{}])[0]
        closes_raw = ((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        spark = [c for c in closes_raw if c is not None][-40:]
        if sym and spark:
            out[sym] = spark
    return out


# ── Public API ─────────────────────────────────────────────────────
async def fetch_tape() -> list[Quote]:
    cached = cache.get("prices:tape")
    if cached is not None:
        return cached

    stooq_syms = [t["stooq"] for t in TAPE if "stooq" in t]
    cg_ids = [t["coingecko"] for t in TAPE if "coingecko" in t]

    async with httpx.AsyncClient() as client:
        stooq_task = _stooq_quotes(client, stooq_syms)
        cg_task = _coingecko_quotes(client, cg_ids)
        stooq_map, cg_map = await asyncio.gather(stooq_task, cg_task)

    quotes: list[Quote] = []
    for t in TAPE:
        if "stooq" in t:
            res = stooq_map.get(t["stooq"])
            if not res:
                continue
            price, change = res
            quotes.append(Quote(symbol=t["stooq"], label=t["label"], price=price, change_pct=change, spark=None))
        else:
            res = cg_map.get(t["coingecko"])
            if not res:
                continue
            price, change, spark = res
            quotes.append(Quote(symbol=t["coingecko"], label=t["label"], price=price, change_pct=change, spark=spark))

    cache.set("prices:tape", quotes, 60)
    return quotes


async def fetch_sparks(symbols: list[str]) -> dict[str, list[float] | None]:
    """Best-effort sparklines (Yahoo). Empty values when throttled."""
    key = "prices:sparks:" + ",".join(sorted(symbols))
    cached = cache.get(key)
    if cached is not None:
        return cached
    async with httpx.AsyncClient() as client:
        sparks = await _yahoo_sparks(client, symbols)
    out: dict[str, list[float] | None] = {s: sparks.get(s) for s in symbols}
    cache.set(key, out, 300)
    return out

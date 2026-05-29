"""Detect ticker symbols inside news titles + fetch sparklines for them.

Lightweight: a hand-tuned alias table keeps signal high and noise low
(`AI` and `OR` would explode if we just regex-scanned uppercase tokens).
"""

from __future__ import annotations

import re

from backend.sources import prices


# title-word → Yahoo symbol
ALIASES: dict[str, str] = {
    # US tech
    "nvidia": "NVDA", "nvda": "NVDA",
    "amd": "AMD",
    "tsmc": "TSM", "tsm": "TSM",
    "intel": "INTC", "intc": "INTC",
    "microsoft": "MSFT", "msft": "MSFT",
    "apple": "AAPL", "aapl": "AAPL",
    "google": "GOOGL", "alphabet": "GOOGL", "googl": "GOOGL",
    "meta": "META",
    "amazon": "AMZN", "amzn": "AMZN",
    "tesla": "TSLA", "tsla": "TSLA",
    "broadcom": "AVGO", "avgo": "AVGO",
    "oracle": "ORCL",
    "openai": "MSFT",   # proxy
    "anthropic": "GOOGL",  # proxy
    # Korea
    "samsung": "005930.KS",
    "sk hynix": "000660.KS", "hynix": "000660.KS",
    "hyundai": "005380.KS",
    "lg": "066570.KS",
    "kospi": "^KS11",
    # Indices / FX
    "s&p": "^GSPC", "s&p 500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "usd/krw": "KRW=X", "won": "KRW=X",
    # Crypto (Yahoo treats these too)
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    # Other
    "trump media": "DJT", "djt": "DJT",
    "doge": "DOGE-USD",
}


_WORD_RE = re.compile(r"[a-z][a-z0-9&./\- ]*", re.IGNORECASE)


def detect_tickers(text: str) -> list[str]:
    """Return up to 2 unique tickers detected in `text`."""
    if not text:
        return []
    low = text.lower()
    out: list[str] = []
    seen: set[str] = set()
    # Longer keys first so "sk hynix" matches before "lg".
    for key in sorted(ALIASES.keys(), key=len, reverse=True):
        if key in low:
            sym = ALIASES[key]
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
        if len(out) >= 2:
            break
    return out


async def enrich_with_sparks(news_items: list[dict]) -> None:
    """Mutate news items in place: add `tickers` and `sparks` arrays."""
    wanted: dict[str, None] = {}
    per_item: list[list[str]] = []
    for item in news_items:
        syms = detect_tickers(item.get("title", ""))
        per_item.append(syms)
        for s in syms:
            wanted[s] = None

    if not wanted:
        for item, syms in zip(news_items, per_item):
            item["tickers"] = syms
            item["sparks"] = {}
        return

    sparks_map = await prices.fetch_sparks(list(wanted.keys()))

    for item, syms in zip(news_items, per_item):
        item["tickers"] = syms
        item["sparks"] = {s: sparks_map.get(s) for s in syms if sparks_map.get(s)}

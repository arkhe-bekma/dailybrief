"""Normalize news / whales / trades / videos into a single ranked list.

Each item ends up with a uniform shape:
  { kind: 'news'|'whale'|'trade'|'video', score: int, ts: iso, ...payload }

`score` is normalized 0–100 so heterogeneous items can be ranked together.
"""

from __future__ import annotations

import html as _html
import re as _re
from datetime import datetime, timezone


_HTML_TAG_RE = _re.compile(r"<[^>]+>")
_ATTR_FRAGMENT_RE = _re.compile(
    r"\b(?:img|src|alt|width|height|border|style|class|figure|figcaption)\s*=\s*['\"][^'\"]*['\"]?",
    _re.IGNORECASE,
)
_ORPHAN_OPEN_TAG_RE = _re.compile(
    r"<\s*(img|figure|figcaption|span|div|p|br|table|tr|td|a|iframe|script|style)\b[^<]*",
    _re.IGNORECASE,
)
_WS_RE = _re.compile(r"\s+")


def _clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    s = _HTML_TAG_RE.sub(" ", raw)
    s = _html.unescape(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    s = _ORPHAN_OPEN_TAG_RE.sub(" ", s)
    s = _ATTR_FRAGMENT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _age_hours(iso: str) -> float:
    try:
        return max(0.0, (_now() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) / 3600)
    except Exception:
        return 999.0


def _decay(score: float, age_h: float, half_life_h: float = 12.0) -> float:
    # Exponential-ish decay so fresh items float up.
    return score * (0.5 ** (age_h / half_life_h))


def from_news(items: list[dict]) -> list[dict]:
    out = []
    for a in items:
        base = a.get("score") or 50
        s = _decay(base, _age_hours(a.get("published", "")))
        out.append({
            "kind": "news",
            "score": round(s),
            "ts": a.get("published"),
            "title": _clean_text(a.get("title")),
            "dek": _clean_text(a.get("summary")),
            "url": a.get("url"),
            "outlet": a.get("outlet"),
            "category": a.get("category"),
            "lang": a.get("lang", "en"),
            "image": a.get("image"),
            "why": a.get("why"),
            "tickers": a.get("tickers", []),
            "sparks": a.get("sparks", {}),
        })
    return out


def from_whales(items: list[dict]) -> list[dict]:
    out = []
    for w in items:
        # Score by size: $5M→25, $20M→50, $100M→75, $500M+→100
        amt = w.get("amount_usd", 0)
        base = min(100, 25 + 25 * (amt / 20_000_000))
        s = _decay(base, _age_hours(w.get("timestamp", "")), half_life_h=6)
        title = f"{w['asset']} {_fmt_usd(amt)} · {w['from_label']} → {w['to_label']}"
        out.append({
            "kind": "whale",
            "score": round(s),
            "ts": w.get("timestamp"),
            "title": title,
            "url": w.get("tx_url"),
            "asset": w.get("asset"),
            "amount_usd": amt,
        })
    return out


def from_trades(items: list[dict]) -> list[dict]:
    out = []
    band_scores = {
        "$1M–$5M": 95, "$500K–$1M": 85, "$250K–$500K": 75,
        "$100K–$250K": 65, "$50K–$100K": 55, "$15K–$50K": 45,
    }
    for t in items:
        base = band_scores.get(t.get("size_band", ""), 50)
        s = _decay(base, _age_hours(t.get("timestamp", "")), half_life_h=24)
        title = f"{t['name']} {t['action']} {t['ticker']} · {t['company']}"
        out.append({
            "kind": "trade",
            "score": round(s),
            "ts": t.get("timestamp"),
            "title": title,
            "url": t.get("source_url"),
            "action": t.get("action"),
            "ticker": t.get("ticker"),
            "role": t.get("role"),
            "size_band": t.get("size_band"),
        })
    return out


def from_videos(items: list[dict]) -> list[dict]:
    out = []
    for v in items:
        s = _decay(70, _age_hours(v.get("published", "")), half_life_h=24)
        out.append({
            "kind": "video",
            "score": round(s),
            "ts": v.get("published"),
            "title": v.get("title"),
            "url": v.get("url"),
            "channel": v.get("channel"),
            "image": v.get("thumbnail"),
        })
    return out


def _fmt_usd(n: float) -> str:
    if n >= 1e9: return f"${n / 1e9:.1f}B"
    if n >= 1e6: return f"${n / 1e6:.1f}M"
    if n >= 1e3: return f"${n / 1e3:.0f}K"
    return f"${n:.0f}"


def merge(*streams: list[dict]) -> list[dict]:
    flat = [item for s in streams for item in s]
    flat.sort(key=lambda x: x["score"], reverse=True)
    return flat

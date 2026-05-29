"""Article curator.

Two modes:
  1. With ANTHROPIC_API_KEY set -> ask Claude to score each article against
     the user's interest profile, return top N + a 1-line "why this matters".
  2. Without a key -> fallback: keyword-overlap scoring, no rewrites.
"""

from __future__ import annotations

import json
import os
from typing import Any

from backend import config


def _heuristic_rank(articles: list[Any], top_k: int) -> list[dict]:
    """Cheap keyword-overlap scorer. No LLM calls."""
    keywords = [k.lower() for k in config.PROFILE.keywords]
    scored: list[tuple[int, Any]] = []
    for a in articles:
        haystack = f"{a.title} {a.summary}".lower()
        hits = sum(1 for k in keywords if k in haystack)
        # Base 50 so news competes with whales/trades when the LLM is off.
        score = min(95, 50 + hits * 9)
        scored.append((score, a))
    scored.sort(key=lambda t: (t[0], t[1].published), reverse=True)
    return [
        {**a.to_dict(), "score": s, "why": None}
        for s, a in scored[:top_k]
    ]


async def _llm_rank(articles: list[Any], top_k: int) -> list[dict]:
    """Ask Claude to rank + add one-line 'why this matters'."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return _heuristic_rank(articles, top_k)

    client = AsyncAnthropic()

    # Compact payload — title + outlet + index. Body content is bulky and
    # mostly redundant for ranking.
    candidates = [
        {"i": i, "title": a.title, "outlet": a.outlet, "category": a.category}
        for i, a in enumerate(articles)
    ]

    prompt = f"""You rank news articles for one specific reader.

READER PROFILE:
{config.PROFILE.bio}

ARTICLES (JSON):
{json.dumps(candidates, ensure_ascii=False)}

Pick the {top_k} most relevant articles for this reader. For each, return:
  - i: original index
  - score: 0-100 (how well it matches the profile)
  - why: one short sentence (<=18 words) explaining why this reader should care

Return ONLY a JSON array, no prose. Example:
[{{"i": 3, "score": 92, "why": "Direct read on Samsung HBM share vs SK hynix."}}]
"""

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    # Strip code fences if Claude adds them.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        return _heuristic_rank(articles, top_k)

    out: list[dict] = []
    for pick in picks[:top_k]:
        idx = pick.get("i")
        if idx is None or idx < 0 or idx >= len(articles):
            continue
        a = articles[idx]
        out.append({
            **a.to_dict(),
            "score": pick.get("score", 0),
            "why": pick.get("why"),
        })
    return out


async def rank(articles: list[Any], top_k: int = 12) -> list[dict]:
    if not articles:
        return []
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return await _llm_rank(articles, top_k)
        except Exception as exc:
            print(f"[curator] LLM rank failed, falling back: {exc}")
    return _heuristic_rank(articles, top_k)

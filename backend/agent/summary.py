"""Daily 'what happened today' headline summary.

Sends the top mixed items to Claude and asks for 2-3 punchy sentences
tailored to the reader's interest profile, written in the reader's primary
language (config.PROFILE.primary_lang).

Falls back to None if no ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import json
import os

from backend import config


_LANG_NAMES = {
    "ko": "Korean (한국어)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Chinese (中文)",
}


_PROMPT = """You are writing one tiny "what happened today" briefing for one specific reader.

READER PROFILE:
{bio}

WRITE THE BRIEFING IN: {lang_name}.

TOP ITEMS RIGHT NOW (mixed: news, whale moves, politician trades, videos):
{items}

Write 2-3 short sentences (<=180 chars total). One paragraph, no bullets, no
preamble. Tone: a sharp friend who reads the tape. Mention the most relevant
1-2 specific things by name. Plain text only. Write in {lang_name}."""


async def generate(mixed: list[dict]) -> str | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    if not mixed:
        return None

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None

    items_payload = [
        {
            "kind": m["kind"],
            "score": m.get("score"),
            "title": m.get("title"),
            "outlet": m.get("outlet") or m.get("channel") or m.get("asset") or m.get("ticker"),
            "lang": m.get("lang"),
        }
        for m in mixed[:25]
    ]

    # Cache the headline by hash of the top-25 titles. Two-min auto-
    # refresh + same top stories → same headline → don't re-bill.
    import hashlib
    from backend import cache as _cache
    title_blob = "|".join(str(i.get("title") or "") for i in items_payload)
    cache_key = f"summary:headline:{hashlib.md5(title_blob.encode()).hexdigest()}"
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit

    lang = getattr(config.PROFILE, "primary_lang", "en")
    lang_name = _LANG_NAMES.get(lang, "English")

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    bio=config.PROFILE.bio,
                    lang_name=lang_name,
                    items=json.dumps(items_payload, ensure_ascii=False),
                ),
            }],
        )
        # Cost telemetry — fire-and-forget.
        try:
            from backend import db as _db
            u = getattr(resp, "usage", None)
            await _db.log_api_call(
                provider="claude-haiku-4-5",
                purpose="headline",
                input_tokens=(getattr(u, "input_tokens", 0) or 0) if u else 0,
                output_tokens=(getattr(u, "output_tokens", 0) or 0) if u else 0,
            )
        except Exception:
            pass
        headline = resp.content[0].text.strip()
        _cache.set(cache_key, headline, 3600)  # 1h
        return headline
    except Exception as exc:
        print(f"[summary] failed: {exc}")
        return None

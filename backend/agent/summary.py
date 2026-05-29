"""Daily 'what happened today' headline summary.

Sends the top mixed items to Claude and asks for 2-3 punchy sentences
tailored to the user's interest profile.

Falls back to None if no ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import json
import os

from backend import config


_PROMPT = """You are writing one tiny "what happened today" briefing for one specific reader.

READER PROFILE:
{bio}

TOP ITEMS RIGHT NOW (mixed: news, whale moves, politician trades, videos):
{items}

Write 2-3 short sentences (<=180 chars total). One paragraph, no bullets, no
preamble. Tone: a sharp friend who reads the tape. Mention the most relevant
1-2 specific things by name. Plain text only."""


async def generate(mixed: list[dict]) -> str | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    if not mixed:
        return None

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None

    # Compact the payload — only what's needed to write the brief.
    items_payload = [
        {
            "kind": m["kind"],
            "score": m.get("score"),
            "title": m.get("title"),
            "outlet": m.get("outlet") or m.get("channel") or m.get("asset") or m.get("ticker"),
        }
        for m in mixed[:25]
    ]

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    bio=config.PROFILE.bio,
                    items=json.dumps(items_payload, ensure_ascii=False),
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"[summary] failed: {exc}")
        return None

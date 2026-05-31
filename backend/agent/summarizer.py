"""Summarizer — turn a long article body into a TL;DR + key points +
short paragraphs, in the article's own language.

Uses Claude Haiku when ANTHROPIC_API_KEY is set; falls back to a tiny
heuristic that just keeps the first few paragraphs verbatim otherwise.
"""

from __future__ import annotations

import json
import os


LANG_NAMES = {
    "ko": "Korean (한국어)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Chinese (中文)",
}


PROMPT = """You are summarizing a news article for a busy reader.

ARTICLE TITLE:
{title}

ARTICLE BODY (may be truncated):
{body}

Return ONLY a valid JSON object with EXACTLY these three keys, no
preamble, no markdown fences:

- "tldr": ONE sentence (≤30 words) capturing the absolute core.
- "key_points": array of 3-5 short bullet strings, each ≤14 words,
  focusing on facts the reader should walk away knowing.
- "paragraphs": array of 2-4 short paragraphs, each ≤80 words, that
  walk the reader through the article's most important content in
  order. Prefer concrete numbers, names, dates.

Write EVERYTHING in {lang_name}. Do not translate names or tickers.
"""


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip("`").strip()
    return s


async def summarize(text: str, title: str, lang: str = "en") -> dict:
    """Returns {tldr, key_points[], paragraphs[]}."""
    empty = {"tldr": None, "key_points": [], "paragraphs": []}
    if not text:
        return empty

    if not os.getenv("ANTHROPIC_API_KEY"):
        # Heuristic fallback: split into chunks, keep the first few.
        paras = [
            p.strip() for p in text.split("\n")
            if p.strip() and len(p.strip()) > 40
        ][:4]
        return {
            "tldr": (paras[0][:160] + "…") if paras else None,
            "key_points": [],
            "paragraphs": paras,
        }

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return empty

    client = AsyncAnthropic()
    lang_name = LANG_NAMES.get(lang, "English")

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1600,
            messages=[{
                "role": "user",
                "content": PROMPT.format(
                    title=title,
                    body=text[:10000],
                    lang_name=lang_name,
                ),
            }],
        )
        raw = _strip_fences(resp.content[0].text)
        data = json.loads(raw)
        return {
            "tldr": data.get("tldr"),
            "key_points": data.get("key_points") or [],
            "paragraphs": data.get("paragraphs") or [],
        }
    except Exception as exc:
        print(f"[summarizer] failed: {exc}")
        # Fall back to heuristic if the LLM blows up
        paras = [p.strip() for p in text.split("\n") if p.strip() and len(p.strip()) > 40][:4]
        return {
            "tldr": (paras[0][:160] + "…") if paras else None,
            "key_points": [],
            "paragraphs": paras,
        }

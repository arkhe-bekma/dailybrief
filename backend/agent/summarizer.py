"""Clean, consistent article rewrite.

Same input, same output shape, for every article — so the reader UI
never gets a surprise emoji bomb or a section header from one outlet's
home-baked AI summary.

Output: a list of 3-5 plain-text paragraphs in the article's own
language. No TL;DR. No bullet points. No emojis. No markdown.

Uses Claude Haiku 4.5 when ANTHROPIC_API_KEY is set (about $0.001 per
article, very cheap). Returns None otherwise; the caller falls back to
the heuristic `_split_paragraphs` in main.py.
"""

from __future__ import annotations

import json
import os
import re


_LANG_NAMES = {
    "ko": "Korean (한국어)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Chinese (中文)",
}


PROMPT = """Rewrite this news article as a concise, easy-to-read summary.

ARTICLE TITLE:
{title}

ARTICLE BODY (may include the publisher's own AI analysis, photo credits,
section headers, emojis — IGNORE that noise and write your own):
{body}

Rules — apply to every output, identical format every time:

1. Write 3-5 short paragraphs, each 40-90 words.
2. Paragraph 1: who, what, when, why — the core news.
3. Paragraphs 2-5: key facts in plain reading order (no bullets).
4. Plain text ONLY. No emojis. No markdown. No "TL;DR". No section
   headers like "Background:" or "1. Analysis". No glossaries.
5. Write in {lang_name}. Keep proper nouns, tickers, and numbers exact.
6. Strip the publisher's own commentary/AI-analysis if present.

Output ONLY a valid JSON object:
{{ "paragraphs": ["…", "…", "…"] }}

No preamble, no code fences, no trailing prose. Just the JSON object.
"""


# Defense in depth — strip emojis from the model's response too, in case
# it ignores rule 4.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "☀-➿"
    "️"
    "‍"
    "]+"
)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip("`").strip()
    return s


async def summarize_paragraphs(text: str, title: str, lang: str = "en") -> list[str] | None:
    """Returns 3-5 cleaned paragraphs, or None if no LLM available / failed."""
    if not text or not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None

    client = AsyncAnthropic()
    lang_name = _LANG_NAMES.get(lang, "English")

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
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
        paras = data.get("paragraphs") or []
        # Belt and suspenders: scrub any stray emojis from Claude's output.
        cleaned = []
        for p in paras:
            if not isinstance(p, str):
                continue
            p = _EMOJI_RE.sub("", p).strip()
            if len(p) >= 30:
                cleaned.append(p)
        return cleaned[:5] if cleaned else None
    except Exception as exc:
        print(f"[summarizer] failed: {exc}")
        return None

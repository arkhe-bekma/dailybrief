"""Article translator agent.

Takes a reader payload (title + paragraphs in some source language) and
returns a translated version. Drives the "translate" button in the
reader modal.

Design notes:
  - Uses Claude Haiku 4.5 (cheap, fast, fluent KR/EN).
  - The prompt asks the model to MATCH the article's energy / tone /
    register — a hard-news lead stays formal, an op-ed stays opinionated,
    an entertainment headline stays punchy. Newspaper tone is the default.
  - Mixing the source language IN where it reads more naturally is
    explicitly allowed. Names, technical terms, ticker symbols, brand
    names, and English jargon that's already in common KR usage
    (예: "AI", "GPU", "ETF", "SaaS") stay in source form.
  - The model may compress a long article into a shorter translation if
    that produces a more readable result. We expose the compression
    in the response so the UI can label it.
  - Cached in SQLite keyed by (url, target_lang). Re-translation
    only happens if the source paragraphs change.

Falls back to None (UI shows an error toast) when ANTHROPIC_API_KEY
is missing or the model errors out.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from backend import cache, db


LANG_NAMES = {
    "ko": "Korean (한국어)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Chinese (中文)",
    "es": "Spanish",
    "fr": "French",
}


_PROMPT = """You are a bilingual newspaper translator. Translate the
following article from {src_name} into {tgt_name}.

ARTICLE:
TITLE: {title}
BODY:
{body}

REQUIREMENTS:
- Use newspaper register by default, but READ the article first and
  match the energy of the original. A market-flash translation should
  feel urgent; an op-ed should feel argued; a K-pop card should keep
  its playful tone. Don't sterilise vivid writing.
- It is FINE to keep proper nouns, technical terms, ticker symbols,
  brand names, and well-known English acronyms (AI, GPU, ETF, SaaS,
  IPO …) in the source language. Mixing the two languages where it
  reads more naturally is preferred over forced literal translation.
- If the article is long and rambling, you MAY compress it — keep all
  load-bearing facts (numbers, names, dates, direct quotes) and drop
  filler. Aim for ~70-90% of the original length when summarising;
  full translation otherwise.
- Do NOT add commentary, hedges, or "as a translator I …" framing.
  Output reads like a newspaper, not like a model.
- Preserve paragraph structure (~one input paragraph → one output
  paragraph), unless compression merges or drops a paragraph.

OUTPUT — strict JSON, no prose around it:
{{
  "title": "<translated title>",
  "paragraphs": ["<para 1>", "<para 2>", ...],
  "summarized": <true if you compressed, false if straight translation>,
  "note": "<optional 1-line translator note in {tgt_name}, or empty>"
}}
"""


def _cache_key(url: str, target_lang: str, body_hash: str) -> str:
    return f"translate:{target_lang}:{body_hash}:{url}"


def _body_hash(title: str, paragraphs: list[str]) -> str:
    """Fingerprint the source text so cached translations are invalidated
    when the reader extractor returns a different body for the same URL."""
    h = hashlib.md5()
    h.update((title or "").encode("utf-8", "ignore"))
    for p in paragraphs or []:
        h.update(b"\n")
        h.update(p.encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


async def translate(
    payload: dict, target_lang: str = "ko",
) -> dict | None:
    """Translate a reader payload into `target_lang`. Returns a dict in
    the same shape as the reader response (title + paragraphs + url +
    image + lang + word_count) plus a `summarized` flag and an optional
    `note` from the translator.

    Returns None when no LLM key is configured."""
    if not payload or not isinstance(payload, dict):
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None

    title = payload.get("title") or ""
    paragraphs = payload.get("paragraphs") or []
    src_lang = payload.get("lang") or "en"
    tgt_lang = target_lang or "ko"
    if src_lang == tgt_lang:
        # Asking for the same language as the source is a no-op.
        return {**payload, "translated": False, "target_lang": tgt_lang}

    bh = _body_hash(title, paragraphs)
    url = payload.get("url") or ""
    ck = _cache_key(url, tgt_lang, bh)

    # Memory cache hit?
    hit = cache.get(ck)
    if hit is not None:
        return hit

    # Disk-backed cache via the reader_results table (re-using the same
    # url+payload shape; we just stamp a "translated_…" prefix on URL).
    disk_url = f"translated::{tgt_lang}::{bh}::{url}"
    disk_hit = await db.get_reader(disk_url)
    if disk_hit is not None:
        cache.set(ck, disk_hit, 7 * 86_400)
        return disk_hit

    client = AsyncAnthropic()
    src_name = LANG_NAMES.get(src_lang, src_lang)
    tgt_name = LANG_NAMES.get(tgt_lang, tgt_lang)

    # Cap body length sent to the model — protects token spend on
    # mega-long articles and matches what the reader modal renders.
    body_text = "\n\n".join(p for p in paragraphs if p)[:8000]

    prompt = _PROMPT.format(
        src_name=src_name, tgt_name=tgt_name,
        title=title, body=body_text,
    )

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip code fences if Claude added them.
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("`").strip()
        parsed = json.loads(text)
    except Exception as exc:
        print(f"[translator] failed for {url[:60]}: {exc!r}", flush=True)
        return None

    out_paragraphs = parsed.get("paragraphs") or []
    if not out_paragraphs:
        return None

    result = {
        "url": url,
        "title": parsed.get("title") or title,
        "image": payload.get("image"),
        "byline": payload.get("byline"),
        "excerpt": payload.get("excerpt"),
        "lang": tgt_lang,
        "source_lang": src_lang,
        "target_lang": tgt_lang,
        "word_count": sum(len(p.split()) for p in out_paragraphs),
        "paragraphs": out_paragraphs,
        "translated": True,
        "summarized": bool(parsed.get("summarized")),
        "note": parsed.get("note") or "",
    }

    # Cache both tiers.
    cache.set(ck, result, 7 * 86_400)
    try:
        await db.save_reader(disk_url, result)
    except Exception as exc:
        print(f"[translator] disk cache save failed: {exc!r}", flush=True)

    return result


async def translate_url(url: str, target_lang: str = "ko") -> dict | None:
    """Convenience wrapper: load the reader payload from disk cache
    and translate it. Returns None if the article hasn't been read
    yet — the caller should hit /api/article first."""
    payload = await db.get_reader(url)
    if not payload:
        return None
    return await translate(payload, target_lang)

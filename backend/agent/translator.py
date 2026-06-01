"""Article translator agent — Gemini Flash edition.

DESIGN NOTES
============

  - LLM:  google-genai SDK calling `gemini-2.5-flash`. Free tier on
          Google AI Studio is plenty for personal use (15 RPM, 1500
          RPD as of 2025). Reads GEMINI_API_KEY (also accepts
          GOOGLE_API_KEY as a synonym).

  - CACHE-FIRST POLICY: every translate() call hits two caches before
          touching the network:
            1. in-memory cache.get(...) — instant
            2. SQLite reader_results table — survives restarts AND
               is GLOBAL across users, so if anyone has translated
               this URL into this language before, every subsequent
               viewer gets the cached version free.
          Only on a full miss does the agent call Gemini, and the
          fresh response is written to BOTH caches before returning.

  - PROMPT: newspaper register by default; reads the article first and
          matches the energy/tone of the source (urgent news → urgent
          target; opinion → opinionated; entertainment → playful).
          May keep proper nouns / ticker symbols / common English
          acronyms (AI, GPU, ETF, IPO …) in source form; mixing KO+EN
          is preferred over forced literal translation. May compress
          long articles (~70-90% length) when that reads better, with
          a `summarized` flag in the response.

  - SCHEMA: Gemini is asked for response_mime_type="application/json"
          with an explicit response_schema, so the parser doesn't have
          to defend against prose drift or code fences.
"""

from __future__ import annotations

import asyncio
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

Return the result strictly in the requested JSON schema:
  title       — translated title
  paragraphs  — array of translated paragraph strings, in order
  summarized  — true if you compressed, false if straight translation
  note        — optional 1-line translator note in {tgt_name}, or ""
"""


# Response schema enforced by Gemini's "controlled generation" mode —
# protects against prose drift / code fences.
_TRANSLATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summarized": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["title", "paragraphs"],
}


def _gemini_api_key() -> str | None:
    """GEMINI_API_KEY preferred, GOOGLE_API_KEY accepted for symmetry
    with Google's other libraries."""
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _cache_key(url: str, target_lang: str, body_hash: str) -> str:
    return f"translate:{target_lang}:{body_hash}:{url}"


def _disk_key(url: str, target_lang: str, body_hash: str) -> str:
    """Key for the SQLite reader_results table. The body_hash makes
    cache entries self-invalidate when the source article actually
    changes."""
    return f"translated::{target_lang}::{body_hash}::{url}"


def _body_hash(title: str, paragraphs: list[str]) -> str:
    """Fingerprint the source text so cached translations are
    invalidated when the reader extractor returns a different body
    for the same URL."""
    h = hashlib.md5()
    h.update((title or "").encode("utf-8", "ignore"))
    for p in paragraphs or []:
        h.update(b"\n")
        h.update(p.encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _gemini_translate_sync(prompt: str) -> dict | None:
    """Blocking Gemini call — wrapped in to_thread by the caller so the
    event loop stays responsive. Returns the parsed dict or None on
    any failure."""
    api_key = _gemini_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        print(f"[translator] google-genai import failed: {exc!r}", flush=True)
        return None

    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_TRANSLATE_SCHEMA,
                temperature=0.4,
                max_output_tokens=4096,
            ),
        )
    except Exception as exc:
        print(f"[translator] gemini call failed: {exc!r}", flush=True)
        return None

    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        return None
    # Schema enforcement should already produce clean JSON, but strip
    # accidental fences just in case.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[translator] gemini returned non-JSON: {exc!r}", flush=True)
        return None


async def translate(
    payload: dict, target_lang: str = "ko",
) -> dict | None:
    """Translate a reader payload into `target_lang`. Same return shape
    as /api/article + {translated, summarized, note, target_lang}.

    Cache-first: in-memory → SQLite → Gemini. The API call only fires
    on a full miss, and a successful response is immediately written
    back to BOTH caches so every subsequent viewer of the same URL +
    target_lang gets it free.
    """
    if not payload or not isinstance(payload, dict):
        return None

    title = payload.get("title") or ""
    paragraphs = payload.get("paragraphs") or []
    src_lang = payload.get("lang") or "en"
    tgt_lang = target_lang or "ko"
    if src_lang == tgt_lang:
        return {**payload, "translated": False, "target_lang": tgt_lang}

    url = payload.get("url") or ""
    bh = _body_hash(title, paragraphs)
    mem_key = _cache_key(url, tgt_lang, bh)
    disk_url = _disk_key(url, tgt_lang, bh)

    # Tier 1: in-memory.
    hit = cache.get(mem_key)
    if hit is not None:
        return hit

    # Tier 2: SQLite. Reuses the existing reader_results table — the
    # prefixed URL key keeps it sharing space without a schema change.
    disk_hit = await db.get_reader(disk_url)
    if disk_hit is not None:
        cache.set(mem_key, disk_hit, 7 * 86_400)
        return disk_hit

    # Tier 3: Gemini call. Network + token spend lives only here.
    if not _gemini_api_key():
        return None

    src_name = LANG_NAMES.get(src_lang, src_lang)
    tgt_name = LANG_NAMES.get(tgt_lang, tgt_lang)
    # Cap source length to keep latency + token usage bounded.
    body_text = "\n\n".join(p for p in paragraphs if p)[:8000]
    prompt = _PROMPT.format(
        src_name=src_name, tgt_name=tgt_name,
        title=title, body=body_text,
    )

    parsed = await asyncio.to_thread(_gemini_translate_sync, prompt)
    if not parsed:
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
        "model": "gemini-2.5-flash",
    }

    # Write back to BOTH caches before returning. SQLite is the
    # cross-user / cross-restart store.
    cache.set(mem_key, result, 7 * 86_400)
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

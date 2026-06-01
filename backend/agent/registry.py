"""Agent registry — single source of truth for what each agent does.

Used by the /lab dashboard to display each agent + its role to the user,
and by the workflow picker to decide which agents to run on each
refresh cycle.
"""

from __future__ import annotations


# Each entry is the user-facing description of one agent in the pipeline.
# Keep `key` stable — it's used in workflow definitions and DB rows.
AGENTS: list[dict] = [
    {
        "key": "ingest",
        "name": "Ingest",
        "role": "RSS aggregator",
        "summary": "Fans out to every configured outlet in parallel, normalises titles, "
                   "dates and images, dedupes by URL.",
        "lang": "py",
        "file": "backend/sources/rss.py",
        "always_on": True,
    },
    {
        "key": "curator",
        "name": "Curator",
        "role": "Relevance ranker",
        "summary": "Asks Claude Haiku 4.5 to score every candidate against the reader's "
                   "interest profile and write a one-line 'why this matters'. Falls "
                   "back to keyword overlap when no LLM key is set.",
        "lang": "py",
        "file": "backend/agent/curator.py",
        "always_on": True,
    },
    {
        "key": "sorter",
        "name": "Sorter",
        "role": "Variety + premium balancer",
        "summary": "Enforces per-category quotas, caps any single outlet at 4 stories per "
                   "category, applies premium-outlet weight bumps, and tops up "
                   "categories that fell short. Outputs the final visible page.",
        "lang": "py",
        "file": "backend/agent/sorter.py",
        "always_on": True,
    },
    {
        "key": "reader",
        "name": "Reader",
        "role": "Article body extractor",
        "summary": "Pulls clean reader-mode text via trafilatura when a card is opened. "
                   "Caches results to SQLite so each URL is only scraped once.",
        "lang": "py",
        "file": "backend/agent/reader.py",
        "always_on": True,
    },
    {
        "key": "illustrator",
        "name": "Illustrator",
        "role": "AI image fallback",
        "summary": "Generates an SDXL image when a story has no usable photo and no "
                   "og:image. Off by default — opt-in via workflow.",
        "lang": "py",
        "file": "backend/agent/illustrator.py",
        "always_on": False,
    },
    {
        "key": "summarizer",
        "name": "Headline writer",
        "role": "Top-of-day briefing",
        "summary": "Sends the top 25 mixed items to Claude and asks for a 2-3 sentence "
                   "'what happened today' headline in the reader's primary language.",
        "lang": "py",
        "file": "backend/agent/summary.py",
        "always_on": True,
    },
    {
        "key": "tickers",
        "name": "Ticker enricher",
        "role": "Sparkline attach",
        "summary": "Detects ticker symbols in titles (NVDA, 삼성, BTC…) and attaches a "
                   "5-day Yahoo sparkline so the card can render a tiny price chart.",
        "lang": "py",
        "file": "backend/tickers.py",
        "always_on": True,
    },
]


# Named workflows — each is a list of agent keys to run on every refresh
# cycle. The lab UI lets the user pick one; the refresh agent reads
# `db.get_setting("workflow")` and honours it.
WORKFLOWS: dict[str, dict] = {
    "balanced": {
        "label": "Balanced",
        "description": "Default — runs everything except the AI illustrator. "
                       "Best quality / cost ratio.",
        "agents": ["ingest", "curator", "sorter", "summarizer", "tickers", "reader"],
    },
    "minimal": {
        "label": "Minimal (no LLM)",
        "description": "Ingest + heuristic sorter + sparklines only. Zero "
                       "Anthropic spend. Use when offline / debugging.",
        "agents": ["ingest", "sorter", "tickers", "reader"],
    },
    "premium_first": {
        "label": "Premium-first",
        "description": "Same as balanced but the sorter is told to weight "
                       "premium outlets much more heavily.",
        "agents": ["ingest", "curator", "sorter", "summarizer", "tickers", "reader"],
        "sorter_overrides": {"premium_weight_bonus": 0.5},
    },
    "speed": {
        "label": "Speed",
        "description": "Skip the LLM curator, fall back to keyword ranking. "
                       "Loads in seconds — useful on slow networks.",
        "agents": ["ingest", "sorter", "summarizer", "tickers"],
    },
    "deep": {
        "label": "Deep dive (with illustrator)",
        "description": "Everything turned on, including AI image generation "
                       "for storyless cards. Slow + costs more.",
        "agents": ["ingest", "curator", "sorter", "summarizer",
                   "tickers", "reader", "illustrator"],
    },
}


DEFAULT_WORKFLOW = "balanced"


def workflow_list() -> list[dict]:
    """Lab-friendly list of workflows."""
    return [
        {"key": k, **v} for k, v in WORKFLOWS.items()
    ]


def agents_list() -> list[dict]:
    """Lab-friendly list of agents with their roles."""
    return AGENTS

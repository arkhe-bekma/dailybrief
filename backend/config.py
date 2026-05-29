"""User-tunable config: interest profile + source list.

Edit `INTEREST_PROFILE` to change what the curator boosts.
Edit `OUTLETS` to add/remove RSS sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InterestProfile:
    # Free-form description sent to the LLM curator. The more specific, the better.
    bio: str = (
        "I follow Korea + USA tech, economics, and politics; I also want world news. "
        "I care about LLMs/AI infrastructure (Nvidia, AMD, datacenters, model training), "
        "crypto whale movements, and politicians' stock disclosures (Pelosi, Trump family, etc.). "
        "I prefer concrete market-moving stories over opinion."
    )
    # Hard keyword boosts (used as a fallback when no ANTHROPIC_API_KEY is set).
    keywords: list[str] = field(default_factory=lambda: [
        "korea", "samsung", "sk hynix", "hyundai", "tsmc",
        "nvidia", "amd", "openai", "anthropic", "datacenter", "gpu",
        "fed", "rate", "inflation", "earnings",
        "trump", "biden", "pelosi", "congress",
        "bitcoin", "ethereum", "whale", "etf",
    ])


PROFILE = InterestProfile()


# ── Outlets ────────────────────────────────────────────────────────────────
# Mix of major Western, Korean, and tech outlets. Reuters' public RSS was
# retired so we route through Google News; everything else is direct.

OUTLETS: list[dict] = [
    # World / general
    {"name": "BBC",          "category": "world",   "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "CNN",          "category": "world",   "url": "http://rss.cnn.com/rss/cnn_topstories.rss"},
    {"name": "NYT",          "category": "world",   "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    {"name": "Guardian",     "category": "world",   "url": "https://www.theguardian.com/world/rss"},
    {"name": "Al Jazeera",   "category": "world",   "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Reuters",      "category": "world",   "url": "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en"},

    # USA economics / business
    {"name": "WSJ",          "category": "econ",    "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml"},
    {"name": "Bloomberg",    "category": "econ",    "url": "https://news.google.com/rss/search?q=site:bloomberg.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "FT",           "category": "econ",    "url": "https://www.ft.com/?format=rss"},

    # Tech
    {"name": "The Verge",    "category": "tech",    "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "category": "tech",    "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "TechCrunch",   "category": "tech",    "url": "https://techcrunch.com/feed/"},
    {"name": "Hacker News",  "category": "tech",    "url": "https://news.ycombinator.com/rss"},

    # Korea
    {"name": "Korea Herald", "category": "korea",   "url": "http://www.koreaherald.com/common/rss_xml.php?ct=102"},
    {"name": "Yonhap",       "category": "korea",   "url": "https://en.yna.co.kr/RSS/news.xml"},
    {"name": "Chosun (EN)",  "category": "korea",   "url": "https://news.google.com/rss/search?q=site:koreajoongangdaily.joins.com&hl=en-US&gl=US&ceid=US:en"},
]


# How many top items per outlet to keep before sending to curator.
PER_OUTLET_LIMIT = 5

# Cache TTL for fetched feeds (seconds)
FEED_CACHE_TTL = 600

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
    # Reader's primary language — used for the daily AI summary headline.
    primary_lang: str = "ko"
    # Hard keyword boosts (fallback when no ANTHROPIC_API_KEY). Mix EN + KO.
    keywords: list[str] = field(default_factory=lambda: [
        "korea", "samsung", "sk hynix", "hyundai", "tsmc",
        "nvidia", "amd", "openai", "anthropic", "datacenter", "gpu",
        "fed", "rate", "inflation", "earnings",
        "trump", "biden", "pelosi", "congress",
        "bitcoin", "ethereum", "whale", "etf",
        # Korean
        "삼성", "하이닉스", "현대", "엔비디아", "테슬라",
        "코스피", "환율", "금리", "연준",
        "트럼프", "바이든", "국회",
        "비트코인", "이더리움", "고래",
    ])


PROFILE = InterestProfile()


# ── Outlets ────────────────────────────────────────────────────────────────
# Mix of major Western, Korean, and tech outlets. Reuters' public RSS was
# retired so we route through Google News; everything else is direct.

OUTLETS: list[dict] = [
    # World / general (en)
    {"name": "BBC",          "category": "world", "lang": "en", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "CNN",          "category": "world", "lang": "en", "url": "http://rss.cnn.com/rss/cnn_topstories.rss"},
    {"name": "NYT",          "category": "world", "lang": "en", "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    {"name": "Guardian",     "category": "world", "lang": "en", "url": "https://www.theguardian.com/world/rss"},
    {"name": "Al Jazeera",   "category": "world", "lang": "en", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Reuters",      "category": "world", "lang": "en", "url": "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en"},

    # USA economics / business (en)
    {"name": "WSJ",          "category": "econ",  "lang": "en", "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml"},
    {"name": "Bloomberg",    "category": "econ",  "lang": "en", "url": "https://news.google.com/rss/search?q=site:bloomberg.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "FT",           "category": "econ",  "lang": "en", "url": "https://www.ft.com/?format=rss"},

    # Tech (en)
    {"name": "The Verge",    "category": "tech",  "lang": "en", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "category": "tech",  "lang": "en", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "TechCrunch",   "category": "tech",  "lang": "en", "url": "https://techcrunch.com/feed/"},
    {"name": "Hacker News",  "category": "tech",  "lang": "en", "url": "https://news.ycombinator.com/rss"},

    # Korea — native language (ko)
    {"name": "조선일보",      "category": "korea", "lang": "ko", "url": "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml"},
    {"name": "한겨레",        "category": "korea", "lang": "ko", "url": "https://www.hani.co.kr/rss/"},
    {"name": "매일경제",      "category": "korea", "lang": "ko", "url": "https://www.mk.co.kr/rss/30000001/"},
    {"name": "매경 증권",     "category": "korea", "lang": "ko", "url": "https://www.mk.co.kr/rss/40300001/"},
    {"name": "SBS 뉴스",     "category": "korea", "lang": "ko", "url": "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=01"},
    {"name": "연합뉴스",      "category": "korea", "lang": "ko", "url": "https://www.yna.co.kr/rss/news.xml"},
    {"name": "연합 경제",     "category": "korea", "lang": "ko", "url": "https://www.yna.co.kr/rss/economy.xml"},
]


# How many top items per outlet to keep before sending to curator.
PER_OUTLET_LIMIT = 5

# Cache TTL for fetched feeds (seconds)
FEED_CACHE_TTL = 600

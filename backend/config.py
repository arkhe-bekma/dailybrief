"""User-tunable config: interest profile + source list.

Edit `INTEREST_PROFILE` to change what the curator boosts.
Edit `OUTLETS` to add/remove RSS sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InterestProfile:
    bio: str = (
        "I follow Korea + USA tech, economics, and politics; I also want world news. "
        "I care about LLMs/AI infrastructure (Nvidia, AMD, datacenters, model training), "
        "crypto whale movements, and politicians' stock disclosures (Pelosi, Trump family, etc.). "
        "I prefer concrete market-moving stories over opinion."
    )
    primary_lang: str = "ko"
    keywords: list[str] = field(default_factory=lambda: [
        # English
        "korea", "samsung", "sk hynix", "hyundai", "tsmc",
        "nvidia", "amd", "openai", "anthropic", "datacenter", "gpu",
        "fed", "rate", "inflation", "earnings",
        "trump", "biden", "pelosi", "congress",
        "bitcoin", "ethereum", "whale", "etf",
        "ai", "llm", "model",
        # Korean
        "삼성", "하이닉스", "현대", "엔비디아", "테슬라", "tsmc",
        "코스피", "환율", "금리", "연준",
        "트럼프", "바이든", "국회",
        "비트코인", "이더리움", "고래",
        "반도체", "ai", "엔비",
    ])


PROFILE = InterestProfile()


# ── Outlets ────────────────────────────────────────────────────────────────
# category + lang power the UI badges and the curator prompt.

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
    {"name": "CNBC",         "category": "econ",  "lang": "en", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"name": "MarketWatch",  "category": "econ",  "lang": "en", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},

    # General tech (en)
    {"name": "The Verge",    "category": "tech",  "lang": "en", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "category": "tech",  "lang": "en", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "TechCrunch",   "category": "tech",  "lang": "en", "url": "https://techcrunch.com/feed/"},
    {"name": "Hacker News",  "category": "tech",  "lang": "en", "url": "https://news.ycombinator.com/rss"},
    {"name": "Tom's Hardware","category": "tech", "lang": "en", "url": "https://www.tomshardware.com/feeds/all"},

    # AI / LLM (en)
    {"name": "OpenAI",       "category": "ai",    "lang": "en", "url": "https://openai.com/blog/rss.xml"},
    {"name": "Hugging Face", "category": "ai",    "lang": "en", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "MIT Tech Rev", "category": "ai",    "lang": "en", "url": "https://www.technologyreview.com/feed/"},
    {"name": "VentureBeat AI","category": "ai",   "lang": "en", "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "SemiWiki",     "category": "ai",    "lang": "en", "url": "https://semiwiki.com/feed/"},

    # Crypto (en)
    {"name": "CoinDesk",     "category": "crypto","lang": "en", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Decrypt",      "category": "crypto","lang": "en", "url": "https://decrypt.co/feed"},
    {"name": "The Block",    "category": "crypto","lang": "en", "url": "https://www.theblock.co/rss.xml"},
    {"name": "Cointelegraph","category": "crypto","lang": "en", "url": "https://cointelegraph.com/rss"},

    # Korea — native (ko)
    {"name": "조선일보",      "category": "korea", "lang": "ko", "url": "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml"},
    {"name": "한겨레",        "category": "korea", "lang": "ko", "url": "https://www.hani.co.kr/rss/"},
    {"name": "동아일보",      "category": "korea", "lang": "ko", "url": "https://www.donga.com/news/rss"},
    {"name": "경향신문",      "category": "korea", "lang": "ko", "url": "https://www.khan.co.kr/rss/rssdata/total_news.xml"},
    {"name": "매일경제",      "category": "korea", "lang": "ko", "url": "https://www.mk.co.kr/rss/30000001/"},
    {"name": "매경 증권",     "category": "korea", "lang": "ko", "url": "https://www.mk.co.kr/rss/40300001/"},
    {"name": "SBS 뉴스",     "category": "korea", "lang": "ko", "url": "https://news.sbs.co.kr/news/SectionRssFeed.do?sectionId=01"},
    {"name": "연합뉴스",      "category": "korea", "lang": "ko", "url": "https://www.yna.co.kr/rss/news.xml"},
    {"name": "연합 경제",     "category": "korea", "lang": "ko", "url": "https://www.yna.co.kr/rss/economy.xml"},
]


# Per-outlet limit before curating. ~15 per outlet × 34 outlets ≈ 510 candidates.
PER_OUTLET_LIMIT = 15

# Cache TTL for fetched feeds (seconds). 1 min — most RSS sources only
# update every couple of minutes anyway, so this is the tightest useful
# value. Per-URL og:image / reader_ok caches still live 24 h, so RSS
# refreshes do NOT trigger fresh probes.
FEED_CACHE_TTL = 60

# How many items the curator returns. Pagination on the client splits these
# into pages — 200 / 30 ≈ 7 pages of content.
TOP_K = 200

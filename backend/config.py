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
#
# Outlets in the PREMIUM_OUTLETS set get a weight bump in the curator and
# are tagged in the DB so the UI can render a ★ pip + use them for the
# "premium picks" rail. Add/remove by display name — case-sensitive.
PREMIUM_OUTLETS: set[str] = {
    # English: top-tier reporting / paywalled / institutional voice
    "Bloomberg", "WSJ", "FT", "NYT", "Reuters", "Nikkei Asia", "Economist",
    "Foreign Affairs", "Foreign Policy", "The Atlantic", "New Yorker",
    "MIT Tech Rev", "HBR", "Nature", "Science Magazine", "Quanta",
    "Ars Technica", "The Verge", "TechCrunch", "MarketWatch",
    "Bloomberg Op-Ed", "NYT Opinion", "WaPo Opinion", "FT Opinion",
    "War on the Rocks", "The Diplomat", "Defense One",
    # Korean: flagship dailies / economic newspapers of record
    "조선일보", "한겨레", "동아일보", "중앙일보", "매일경제", "한국경제",
    "연합뉴스", "연합 경제", "SBS 뉴스", "경향신문", "서울신문",
    "조선 오피니언", "한경 오피니언", "동아 오피니언",
}

# Per-outlet weight multiplier the curator applies before its base score.
# Higher = the outlet's stories float up more, lower = they don't crowd
# the feed. Defaults to 1.0 (neutral). Premium outlets default to 1.25.
OUTLET_WEIGHT: dict[str, float] = {
    # Bumps beyond the premium default — house picks the user explicitly
    # cares about. Curator multiplies score by this value.
    "Bloomberg": 1.45, "FT": 1.40, "Reuters": 1.40, "WSJ": 1.35, "NYT": 1.30,
    "Nikkei Asia": 1.35, "Economist": 1.30, "Foreign Affairs": 1.25,
    "조선일보": 1.30, "한겨레": 1.20, "한국경제": 1.30, "매일경제": 1.30,
    "연합뉴스": 1.30, "연합 경제": 1.30,
    # Light de-emphasis — useful but lower priority than majors.
    "Hacker News": 0.90, "allkpop": 0.85, "Koreaboo": 0.80, "Soompi": 0.85,
    "K-Pop Herald": 0.85, "Hellokpop": 0.80,
}


def outlet_meta(name: str) -> dict:
    """Look up the premium flag + weight for an outlet by display name.
    Used by the curator and the DB writer. Stable across config edits."""
    premium = name in PREMIUM_OUTLETS
    weight = OUTLET_WEIGHT.get(name, 1.25 if premium else 1.0)
    return {"premium": premium, "weight": weight}


OUTLETS: list[dict] = [
    # World / general (en)
    {"name": "BBC",          "category": "world", "lang": "en", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "CNN",          "category": "world", "lang": "en", "url": "http://rss.cnn.com/rss/cnn_topstories.rss"},
    {"name": "NYT",          "category": "world", "lang": "en", "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"},
    {"name": "Guardian",     "category": "world", "lang": "en", "url": "https://www.theguardian.com/world/rss"},
    {"name": "Al Jazeera",   "category": "world", "lang": "en", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Reuters",      "category": "world", "lang": "en", "url": "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "WaPo",         "category": "world", "lang": "en", "url": "https://feeds.washingtonpost.com/rss/world"},
    {"name": "ABC News",     "category": "world", "lang": "en", "url": "https://abcnews.go.com/abcnews/topstories"},
    {"name": "NBC News",     "category": "world", "lang": "en", "url": "https://feeds.nbcnews.com/nbcnews/public/news"},
    {"name": "AP",           "category": "world", "lang": "en", "url": "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "AFP",          "category": "world", "lang": "en", "url": "https://news.google.com/rss/search?q=site:afp.com&hl=en-US&gl=US&ceid=US:en"},

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
    {"name": "Wired",        "category": "tech",  "lang": "en", "url": "https://www.wired.com/feed/rss"},
    {"name": "Engadget",     "category": "tech",  "lang": "en", "url": "https://www.engadget.com/rss.xml"},
    {"name": "AnandTech",    "category": "tech",  "lang": "en", "url": "https://news.google.com/rss/search?q=site:anandtech.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "9to5Mac",      "category": "tech",  "lang": "en", "url": "https://9to5mac.com/feed/"},

    # AI / LLM (en)
    {"name": "OpenAI",       "category": "ai",    "lang": "en", "url": "https://openai.com/news/rss.xml"},
    {"name": "Hugging Face", "category": "ai",    "lang": "en", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "MIT Tech Rev", "category": "ai",    "lang": "en", "url": "https://www.technologyreview.com/feed/"},
    {"name": "VentureBeat AI","category": "ai",   "lang": "en", "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "SemiWiki",     "category": "ai",    "lang": "en", "url": "https://semiwiki.com/feed/"},

    # Crypto (en)
    {"name": "CoinDesk",     "category": "crypto","lang": "en", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Decrypt",      "category": "crypto","lang": "en", "url": "https://decrypt.co/feed"},
    {"name": "The Block",    "category": "crypto","lang": "en", "url": "https://news.google.com/rss/search?q=site:theblock.co&hl=en-US&gl=US&ceid=US:en"},
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
    {"name": "한국경제",      "category": "korea", "lang": "ko", "url": "https://www.hankyung.com/feed/all-news"},
    {"name": "서울신문",      "category": "korea", "lang": "ko", "url": "https://www.seoul.co.kr/xml/rss/rss_economy.xml"},
    # Naver — public RSS retired; use Google News proxy for each section.
    {"name": "네이버 뉴스",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 정치",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+%EC%A0%95%EC%B9%98&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 경제",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+%EA%B2%BD%EC%A0%9C&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 사회",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+%EC%82%AC%ED%9A%8C&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 IT",       "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+IT&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 세계",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+%EC%84%B8%EA%B3%84&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 생활",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:n.news.naver.com+%EC%83%9D%ED%99%9C&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 스포츠",   "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:sports.news.naver.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 부동산",   "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:land.naver.com+OR+site:n.news.naver.com+%EB%B6%80%EB%8F%99%EC%82%B0&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "ZDNet Korea",  "category": "korea", "lang": "ko", "url": "https://feeds.feedburner.com/zdkorea"},
    {"name": "전자신문",      "category": "korea", "lang": "ko", "url": "https://rss.etnews.com/Section902.xml"},
    # Additional Korean mainstream + economic dailies (round out the roster
    # alongside the existing big four). All Google-News-proxied because
    # most KR publishers retired their public RSS several years ago.
    {"name": "중앙일보",      "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:joongang.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "한국일보",      "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:hankookilbo.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "머니투데이",    "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:mt.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "이데일리",      "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:edaily.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "헤럴드경제",    "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:heraldcorp.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "파이낸셜뉴스",  "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:fnnews.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "KBS 뉴스",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:news.kbs.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "MBC 뉴스",     "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:imnews.imbc.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "JTBC 뉴스",    "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:news.jtbc.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "YTN",          "category": "korea", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:ytn.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "Yonhap EN",    "category": "korea", "lang": "en", "url": "https://en.yna.co.kr/RSS/news.xml"},

    # Opinion / Columnists (en) — 칼럼
    {"name": "NYT Opinion",      "category": "opinion", "lang": "en", "url": "https://rss.nytimes.com/services/xml/rss/nyt/Opinion.xml"},
    {"name": "WaPo Opinion",     "category": "opinion", "lang": "en", "url": "https://feeds.washingtonpost.com/rss/opinions"},
    {"name": "Bloomberg Op-Ed",  "category": "opinion", "lang": "en", "url": "https://news.google.com/rss/search?q=site:bloomberg.com/opinion&hl=en-US&gl=US&ceid=US:en"},
    {"name": "FT Opinion",       "category": "opinion", "lang": "en", "url": "https://www.ft.com/opinion?format=rss"},
    {"name": "Economist",        "category": "opinion", "lang": "en", "url": "https://www.economist.com/the-world-this-week/rss.xml"},
    {"name": "Foreign Affairs",  "category": "opinion", "lang": "en", "url": "https://www.foreignaffairs.com/rss.xml"},
    {"name": "The Atlantic",     "category": "opinion", "lang": "en", "url": "https://www.theatlantic.com/feed/all/"},
    {"name": "New Yorker",       "category": "opinion", "lang": "en", "url": "https://www.newyorker.com/feed/everything"},
    # Korean opinion / 칼럼
    {"name": "조선 오피니언",   "category": "opinion", "lang": "ko", "url": "https://www.chosun.com/arc/outboundfeeds/rss/category/opinion/?outputType=xml"},
    {"name": "한경 오피니언",   "category": "opinion", "lang": "ko", "url": "https://www.hankyung.com/feed/opinion"},
    {"name": "동아 오피니언",   "category": "opinion", "lang": "ko", "url": "https://rss.donga.com/editorials.xml"},

    # Science (en) — 과학
    {"name": "Quanta",           "category": "science", "lang": "en", "url": "https://www.quantamagazine.org/feed/"},
    {"name": "Nature",           "category": "science", "lang": "en", "url": "https://www.nature.com/nature.rss"},
    {"name": "Science Magazine", "category": "science", "lang": "en", "url": "https://www.science.org/rss/news_current.xml"},
    {"name": "ScienceDaily",     "category": "science", "lang": "en", "url": "https://www.sciencedaily.com/rss/all.xml"},
    {"name": "Ars Science",      "category": "science", "lang": "en", "url": "https://feeds.arstechnica.com/arstechnica/science"},

    # Business (en) — 비즈니스 (벤처/창업/매니지먼트, econ과 분리)
    {"name": "Forbes",           "category": "biz", "lang": "en", "url": "https://www.forbes.com/business/feed/"},
    {"name": "Business Insider", "category": "biz", "lang": "en", "url": "https://feeds.businessinsider.com/custom/all"},
    {"name": "Fast Company",     "category": "biz", "lang": "en", "url": "https://www.fastcompany.com/latest/rss"},
    {"name": "Inc.",             "category": "biz", "lang": "en", "url": "https://news.google.com/rss/search?q=site:inc.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "HBR",              "category": "biz", "lang": "en", "url": "https://news.google.com/rss/search?q=site:hbr.org&hl=en-US&gl=US&ceid=US:en"},

    # Geopolitics (en) — 지정학 / 외교 / 안보
    {"name": "Foreign Policy",   "category": "geo", "lang": "en", "url": "https://foreignpolicy.com/feed/"},
    {"name": "War on the Rocks", "category": "geo", "lang": "en", "url": "https://warontherocks.com/feed/"},
    {"name": "Defense One",      "category": "geo", "lang": "en", "url": "https://www.defenseone.com/rss/all/"},
    {"name": "The Diplomat",     "category": "geo", "lang": "en", "url": "https://thediplomat.com/feed/"},
    {"name": "Nikkei Asia",      "category": "geo", "lang": "en", "url": "https://asia.nikkei.com/rss/feed/nar"},

    # Korean entertainment — 한국 연예 (kent)
    {"name": "네이버 연예",     "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:entertain.naver.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 연예 종합", "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=%EC%97%B0%EC%98%88+%EC%A2%85%ED%95%A9&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 K-Pop",    "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:entertain.naver.com+%EC%BC%80%EC%9D%B4%ED%8C%9D&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 드라마",    "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:entertain.naver.com+%EB%93%9C%EB%9D%BC%EB%A7%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 영화",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:entertain.naver.com+%EC%98%81%ED%99%94&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "네이버 방송",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:entertain.naver.com+%EB%B0%A9%EC%86%A1&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "디스패치",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:dispatch.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "OSEN",           "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:osen.mt.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "마이데일리",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:mydaily.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "스타뉴스",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:star.mt.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "텐아시아",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:tenasia.hankyung.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "일간스포츠",      "category": "kent", "lang": "ko", "url": "https://isplus.com/rss"},
    {"name": "TV리포트",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:tvreport.co.kr&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "스포츠조선 연예",  "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=%EC%8A%A4%ED%8F%AC%EC%B8%A0%EC%A1%B0%EC%84%A0+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "스포츠경향 연예",  "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:sports.khan.co.kr+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "스포츠동아 연예",  "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:sports.donga.com+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "헤럴드POP",       "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:pop.heraldcorp.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "엑스포츠뉴스",     "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:xportsnews.com&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "뉴스1 연예",       "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:news1.kr+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "뉴시스 연예",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:newsis.com+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "한경연예",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:hankyung.com+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "조선연예",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:chosun.com+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "MBC 연예",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:imnews.imbc.com+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "SBS 연예",        "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:news.sbs.co.kr+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "JTBC 연예",       "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=site:news.jtbc.co.kr+%EC%97%B0%EC%98%88&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "K-Pop 종합",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=K-Pop+OR+%EC%BC%80%EC%9D%B4%ED%8C%9D&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "K-드라마 종합",    "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88+OR+K-Drama&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "K-영화 종합",      "category": "kent", "lang": "ko", "url": "https://news.google.com/rss/search?q=%ED%95%9C%EA%B5%AD%EC%98%81%ED%99%94+OR+%EC%98%81%ED%99%94%EA%B3%84&hl=ko&gl=KR&ceid=KR:ko"},
    # International K-Ent (en) — Soompi + Koreaboo publish proper RSS
    # at their canonical /feed path; their cards get real photos via
    # the publisher's og:image. allkpop + Hellokpop retired their RSS
    # so we still proxy via Google News (image-less, just headline).
    {"name": "Soompi",         "category": "kent", "lang": "en", "url": "https://www.soompi.com/feed"},
    {"name": "Koreaboo",       "category": "kent", "lang": "en", "url": "https://www.koreaboo.com/feed"},
    {"name": "allkpop",        "category": "kent", "lang": "en", "url": "https://news.google.com/rss/search?q=site:allkpop.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Hellokpop",      "category": "kent", "lang": "en", "url": "https://news.google.com/rss/search?q=site:hellokpop.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "K-Pop Herald",   "category": "kent", "lang": "en", "url": "https://news.google.com/rss/search?q=K-Pop+Herald&hl=en-US&gl=US&ceid=US:en"},
]


# ── Category labels — used by the chip nav, the curator prompt, and as
# stable identifiers in API responses. Don't rename keys without a DB
# migration; the `articles.category` column stores these slugs verbatim.
CATEGORY_LABELS: dict[str, dict] = {
    "world":   {"en": "World",       "ko": "월드"},
    "econ":    {"en": "Markets",     "ko": "마켓"},
    "tech":    {"en": "Tech",        "ko": "테크"},
    "ai":      {"en": "AI",          "ko": "AI"},
    "crypto":  {"en": "Crypto",      "ko": "크립토"},
    "korea":   {"en": "Korea",       "ko": "한국"},
    "opinion": {"en": "Opinion",     "ko": "칼럼"},
    "science": {"en": "Science",     "ko": "과학"},
    "biz":     {"en": "Business",    "ko": "비즈니스"},
    "geo":     {"en": "Geopolitics", "ko": "지정학"},
    "kent":    {"en": "K-Ent",       "ko": "K-연예"},
}


# Per-outlet limit before curating. ~10 per outlet × ~80 outlets ≈ 800 candidates.
PER_OUTLET_LIMIT = 10

# Cache TTL for fetched feeds (seconds). 1 min — most RSS sources only
# update every couple of minutes anyway, so this is the tightest useful
# value. Per-URL og:image / reader_ok caches still live 24 h, so RSS
# refreshes do NOT trigger fresh probes.
FEED_CACHE_TTL = 60

# How many items the curator returns. Pagination on the client splits these
# into pages — 250 / 30 ≈ 8 pages of content.
TOP_K = 250

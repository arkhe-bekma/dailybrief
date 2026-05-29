"""YouTube channel uploads strip.

MOCKED. Real wire: YouTube Data API v3 `search.list` per channelId, or pull
each channel's RSS at https://www.youtube.com/feeds/videos.xml?channel_id=...
"""

from dataclasses import dataclass, asdict


@dataclass
class YouTubeItem:
    title: str
    channel: str
    url: str
    thumbnail: str
    published: str

    def to_dict(self) -> dict:
        return asdict(self)


_MOCK = [
    ("Bloomberg",         "Daily Brief: Markets close mixed as Fed signals pause",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
     "2026-05-29T13:00:00Z"),
    ("CNBC",              "Nvidia earnings: AI demand still 'insatiable'?",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
     "2026-05-29T11:30:00Z"),
    ("Arirang News",      "Seoul reacts to new US chip export rules",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
     "2026-05-29T09:15:00Z"),
    ("Lex Fridman",       "On the future of frontier models",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
     "2026-05-28T18:00:00Z"),
    ("All-In Podcast",    "Whale moves, the Trump portfolio, and rate-cut odds",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
     "2026-05-28T16:00:00Z"),
]


async def fetch() -> list[YouTubeItem]:
    return [YouTubeItem(title=t, channel=c, url=u, thumbnail=th, published=p)
            for c, t, u, th, p in _MOCK]

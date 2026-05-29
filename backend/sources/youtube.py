"""YouTube channel uploads strip — REAL feed via per-channel RSS.

No API key needed: YouTube exposes
  https://www.youtube.com/feeds/videos.xml?channel_id=<CHANNEL_ID>
for every channel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import re

import httpx

from backend import cache


@dataclass
class YouTubeItem:
    title: str
    channel: str
    url: str
    thumbnail: str
    published: str

    def to_dict(self) -> dict:
        return asdict(self)


# Curated channel list. Add more by appending {name, channel_id}.
CHANNELS: list[dict] = [
    {"name": "Bloomberg",        "id": "UCIALMKvObZNtJ6AmdCLP7Lg"},
    {"name": "CNBC",             "id": "UCvJJ_dzjViJCoLf5uKUTwoA"},
    {"name": "Reuters",          "id": "UChqUTb7kYRX8-EiaN3XFrSQ"},
    {"name": "Yahoo Finance",    "id": "UCEAZeUIeJs0IjQiqTCdVSIg"},
    {"name": "Lex Fridman",      "id": "UCSHZKyawb77ixDdsGog4iWA"},
    {"name": "All-In Podcast",   "id": "UCESLZhusAkFfsNsApnjF_Cg"},
    {"name": "Two Minute Papers","id": "UCbfYPyITQ-7l4upoX8nvctg"},
]


_VID_RE = re.compile(r"<yt:videoId>([^<]+)</yt:videoId>")
_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
_PUB_RE = re.compile(r"<published>([^<]+)</published>")
_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)


async def _fetch_channel(client: httpx.AsyncClient, ch: dict) -> list[YouTubeItem]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['id']}"
    try:
        r = await client.get(url, timeout=8.0)
        r.raise_for_status()
        text = r.text
    except Exception as exc:
        print(f"[youtube] {ch['name']} failed: {exc}")
        return []

    items: list[YouTubeItem] = []
    for entry in _ENTRY_RE.findall(text)[:3]:  # 3 newest per channel
        vid_m = _VID_RE.search(entry)
        title_m = _TITLE_RE.search(entry)
        pub_m = _PUB_RE.search(entry)
        if not (vid_m and title_m):
            continue
        vid = vid_m.group(1)
        title = title_m.group(1).strip()
        pub = pub_m.group(1) if pub_m else datetime.now(timezone.utc).isoformat()
        items.append(YouTubeItem(
            title=title,
            channel=ch["name"],
            url=f"https://www.youtube.com/watch?v={vid}",
            thumbnail=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            published=pub,
        ))
    return items


async def fetch() -> list[YouTubeItem]:
    cached = cache.get("youtube:list")
    if cached is not None:
        return cached
    async with httpx.AsyncClient() as client:
        batches = await asyncio.gather(*[_fetch_channel(client, c) for c in CHANNELS])
    items = [v for batch in batches for v in batch]
    items.sort(key=lambda v: v.published, reverse=True)
    cache.set("youtube:list", items, 1800)
    return items

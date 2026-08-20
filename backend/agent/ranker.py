"""Ranking department — the '부서' that makes the feed read like a real
news front page instead of a raw reverse-chronological dump.

Runs periodically over the recent active pool and, with NO LLM (fully
deterministic, cheap enough for the 512MB box):

  1. Tags each article's outlet authority (premium / weight) from config.
  2. Clusters near-duplicate stories ACROSS outlets (token-Jaccard on
     titles, within category+language buckets). Each cluster = one
     real-world event covered by 1..N outlets.
  3. Collapses every cluster to its single best article; the losers get
     dup_of = survivor_url and drop out of the feed (still in the DB).
  4. Scores importance = outlet authority + CORROBORATION (how many
     outlets carried the story) + a SLOW recency decay — so big
     multi-outlet stories rank to the front and STAY there longer,
     and the order doesn't reshuffle on every refresh.
  5. Persists score / premium / weight / corroboration / dup_of / ranked_at.

The serving path already orders by (premium DESC, score DESC,
published_at DESC) and hides dup_of rows, so once this runs the feed is
importance-ranked, deduped, and stable with zero further serving changes.
"""

from __future__ import annotations

import re
import time
from contextlib import closing

from backend import db, config


# ── title signature / similarity (self-contained; no dep on dedup.py) ──
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")

# Common words that carry no story identity — dropped before comparing.
_STOPWORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "it", "its", "this", "that", "these", "those", "he", "she",
    "they", "we", "you", "his", "her", "their", "how", "why", "what",
    "who", "when", "where", "will", "would", "can", "could", "says", "say",
    "said", "after", "over", "new", "up", "down", "out", "not", "no",
    "amid", "into", "than", "more", "most", "us", "u", "s",
    # Korean particles / fillers
    "그리고", "그러나", "하지만", "또", "및", "등", "관련", "기자", "속보",
    "종합", "단독", "인터뷰", "오늘", "이번", "지난", "대한", "위해",
}

_MIN_TOKENS = 3          # titles with fewer real tokens are never clustered
_SIM_THRESHOLD = 0.52    # token-Jaccard ≥ this ⇒ same story
_WINDOW_DAYS = 3         # only rank the recent, feed-relevant window
_MAX_ARTICLES = 6000     # hard ceiling on rows loaded per pass
_BUCKET_CAP = 1400       # per (category,lang) bucket cap on pairwise compares

# Baseline importance per desk, so a busy-but-minor desk (kent) doesn't
# outrank a world/geo/econ story purely on volume.
_CATEGORY_BASE = {
    "world": 62, "geo": 62, "econ": 60, "biz": 57, "tech": 55, "ai": 55,
    "science": 54, "crypto": 51, "opinion": 49, "korea": 52, "kent": 45,
    "nature": 47,
}
_DEFAULT_BASE = 50


def _signature(title: str) -> frozenset[str]:
    if not title:
        return frozenset()
    toks = [t.lower() for t in _TOKEN_RE.findall(title)]
    toks = [t for t in toks if t not in _STOPWORDS and len(t) > 1]
    return frozenset(toks)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _age_hours(published_at, fetched_at, now: int) -> float:
    ts = None
    if published_at:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(
                str(published_at).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            ts = None
    if ts is None:
        ts = float(fetched_at or now)
    return max(0.0, (now - ts) / 3600.0)


def _quality_rank(a: dict) -> tuple:
    """Survivor preference: premium first, then a real image, then a
    longer summary, then the most recent. Higher tuple wins."""
    return (
        1 if a.get("premium") else 0,
        1 if (a.get("image") or "") else 0,
        len(a.get("summary") or ""),
        str(a.get("published_at") or ""),
    )


def _score(a: dict, corroboration: int, now: int) -> int:
    base = _CATEGORY_BASE.get(a.get("category") or "", _DEFAULT_BASE)
    premium_bonus = 22 if a.get("premium") else 0
    weight = float(a.get("weight") or 1.0)
    weight_bonus = min(12.0, max(0.0, (weight - 1.0) * 24.0))
    # Corroboration is the headline signal: many outlets on one story ⇒
    # big news. 2 outlets +12, 3 +20, 4 +26, capped so a wire pile-up
    # can't fully pin the top.
    corr_bonus = min(30.0, (corroboration - 1) * 8.0)
    # Slow recency so freshness nudges but doesn't dominate — importance
    # stays put for days rather than reshuffling every ingest.
    age = _age_hours(a.get("published_at"), a.get("fetched_at"), now)
    recency = max(0.0, 16.0 - age * 0.18)          # ~+16 fresh → 0 by ~90h
    return int(max(0, min(100, round(
        base + premium_bonus + weight_bonus + corr_bonus + recency
    ))))


def _cluster_bucket(arts: list[dict]) -> list[list[dict]]:
    """Union-find clustering within one (category,lang) bucket. Bounded
    to the newest _BUCKET_CAP rows so the O(n²) stays cheap."""
    arts = arts[:_BUCKET_CAP]
    n = len(arts)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    sigs = [a["sig"] for a in arts]
    for i in range(n):
        si = sigs[i]
        if len(si) < _MIN_TOKENS:
            continue
        for j in range(i + 1, n):
            sj = sigs[j]
            if len(sj) < _MIN_TOKENS:
                continue
            if _jaccard(si, sj) >= _SIM_THRESHOLD:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for idx, a in enumerate(arts):
        groups.setdefault(find(idx), []).append(a)
    return list(groups.values())


def rank_active(dry_run: bool = False) -> dict:
    now = int(time.time())
    cutoff = now - _WINDOW_DAYS * 86400
    with closing(db._conn()) as c:
        rows = c.execute(
            "SELECT url, outlet, category, lang, title, summary, image, "
            "published_at, fetched_at FROM articles "
            "WHERE archived = 0 AND validated != -1 AND fetched_at >= ? "
            "ORDER BY fetched_at DESC LIMIT ?",
            (cutoff, _MAX_ARTICLES),
        ).fetchall()
        arts = [dict(r) for r in rows]

    # Authority + signature.
    for a in arts:
        meta = config.outlet_meta(a.get("outlet") or "")
        a["premium"] = 1 if meta["premium"] else 0
        a["weight"] = float(meta["weight"])
        a["sig"] = _signature(a.get("title") or "")

    # Bucket by (category, lang) — cross-outlet dupes of one event share
    # both. Cluster within each bucket only.
    buckets: dict[tuple, list[dict]] = {}
    for a in arts:
        buckets.setdefault((a.get("category"), a.get("lang")), []).append(a)

    updates: list[tuple] = []
    clusters_total = collapsed = 0
    score_hist: dict[str, int] = {"0-49": 0, "50-64": 0, "65-79": 0, "80-100": 0}
    for bucket_arts in buckets.values():
        for cluster in _cluster_bucket(bucket_arts):
            size = len(cluster)
            if size > 1:
                clusters_total += 1
            survivor = max(cluster, key=_quality_rank)
            for a in cluster:
                if a is survivor:
                    sc = _score(a, size, now)
                    updates.append((sc, a["premium"], a["weight"], size, None, now, a["url"]))
                    b = ("80-100" if sc >= 80 else "65-79" if sc >= 65
                         else "50-64" if sc >= 50 else "0-49")
                    score_hist[b] += 1
                else:
                    collapsed += 1
                    updates.append((0, a["premium"], a["weight"], size, survivor["url"], now, a["url"]))

    if not dry_run and updates:
        with closing(db._conn()) as c:
            c.executemany(
                "UPDATE articles SET score = ?, premium = ?, weight = ?, "
                "corroboration = ?, dup_of = ?, ranked_at = ? WHERE url = ?",
                updates,
            )
            c.commit()

    return {
        "examined": len(arts),
        "clusters_multi": clusters_total,
        "collapsed_dupes": collapsed,
        "survivors": len(arts) - collapsed,
        "score_hist": score_hist,
        "dry_run": dry_run,
        "took_s": round(time.time() - now, 1),
    }

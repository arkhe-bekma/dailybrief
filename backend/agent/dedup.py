"""Deduplication agent.

The user's ask: when 5 outlets cover the same story, don't list all 5 —
pick the strongest single article (best body, best image, premium
outlet) and at most one backup that offers a meaningfully different
angle or outlet. Drop the rest.

Approach: cheap, deterministic, no LLM call.
  1. Normalize each article's title (strip outlet suffix, brackets,
     punctuation, common stopwords; tokenise) → a set of "signature"
     tokens.
  2. Cluster articles whose signature Jaccard similarity ≥ THRESHOLD.
  3. In each cluster, sort by quality (premium → quality score →
     body length → image presence) and keep the top 2.

Notes & limits:
  - The signature is language-agnostic but token-based, so a KR
    headline and an EN headline of the same story don't dedup against
    each other. That's fine — different language coverage is real
    value, not duplication.
  - O(n²) over the ~250 enriched items per refresh = ~30k comparisons,
    each a hashable-set intersection. Runs in well under 100 ms.
  - Lives between enrich_top and sorter.balance — the deduplicator
    cuts the candidate pool so per-category quotas don't get filled
    with five Reuters/AP/Bloomberg copies of the same story.
"""

from __future__ import annotations

import re
from collections import defaultdict

from backend import config


SIMILARITY_THRESHOLD = 0.30      # 30% — more aggressive than the prior 0.42.
                                 # The user explicitly reported same-story
                                 # repeats slipping through; this widens the
                                 # net to catch "Trump says X" / "Trump's X
                                 # remarks" / "Trump comments on X" clusters
                                 # that 0.42 was missing.
MAX_PER_CLUSTER = 1              # Keep only the single best per cluster.
                                 # Spec: "같은 내용의 기사들 제발 좀 쳐내줘"
                                 # — drop, don't keep a backup.
MIN_SIGNATURE_TOKENS = 3         # don't dedup ultra-short titles


# Outlet-name suffixes Google News appends to article titles. Drop so
# the comparison is between the underlying headline, not the publisher.
_OUTLET_SUFFIX_RE = re.compile(
    r"\s*[-—·|]\s*(?:[A-Z][\w&'.]*\s*){1,4}$"
)
# Throwaway tokens (low signal, present in many unrelated titles).
_STOPWORDS = {
    # English fillers + news-y verbs
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "for", "by", "with", "from", "as",
    "and", "or", "but", "if", "then", "than", "this", "that", "these",
    "those", "it", "its", "his", "her", "their", "our", "you", "your",
    "us", "we", "they", "them", "him", "she", "he", "i",
    "says", "said", "say", "tells", "told", "reports", "report",
    "vs", "via", "amid", "after", "before", "into", "over", "under",
    # Korean fillers — these are particles + light verbs that show up
    # in roughly every headline and would otherwise inflate similarity.
    "는", "은", "이", "가", "을", "를", "에", "에서", "와", "과",
    "도", "만", "의", "로", "으로", "한", "하다", "한다", "했다",
    "있다", "없다", "이다", "라고", "라며", "이라", "이라는",
    "vs", "및", "또", "또는",
}
# Bracketed prefixes like [속보], [BREAKING], etc. — strip before tokenising.
_BRACKET_PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]\s*")
# Anything not a word character or Korean syllable / Latin letter.
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]+")


def _signature(title: str) -> frozenset[str]:
    """Lowercase, strip outlet suffix and bracket prefix, tokenise,
    drop stopwords. Returns a frozenset of remaining tokens."""
    if not title:
        return frozenset()
    s = _BRACKET_PREFIX_RE.sub("", title)
    s = _OUTLET_SUFFIX_RE.sub("", s)
    s = s.lower()
    tokens = [t for t in _TOKEN_RE.findall(s) if t and t not in _STOPWORDS]
    # Single-character Latin tokens are noise; keep Korean singletons
    # since one syllable can carry meaning.
    tokens = [t for t in tokens if len(t) > 1 or "가" <= t <= "힣"]
    return frozenset(tokens)


def _quality_rank(item: dict) -> float:
    """Higher = better. Used to pick the survivor in each cluster."""
    outlet_meta = config.outlet_meta(item.get("outlet") or "")
    score = 0.0
    score += float(item.get("quality") or 0)        # sorter-supplied
    score += 80.0 if outlet_meta.get("premium") else 0.0
    score += 30.0 if item.get("image") else 0.0
    summary_len = len(item.get("summary") or "")
    score += min(40.0, summary_len / 10.0)           # 400 chars → +40
    score += min(20.0, float(item.get("score") or 0) * 0.2)
    return score


def deduplicate(items: list[dict]) -> tuple[list[dict], dict]:
    """Cluster `items` by title similarity, keep at most MAX_PER_CLUSTER
    survivors per cluster. Returns (deduped_items, stats) where stats
    has {clusters, removed, kept_singles}."""
    if not items:
        return items, {"clusters": 0, "removed": 0, "kept_singles": 0}

    signatures: list[tuple[int, frozenset[str], dict]] = []
    for i, it in enumerate(items):
        sig = _signature(it.get("title") or "")
        signatures.append((i, sig, it))

    # Union-find over indices.
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # Pairwise comparison. Skip pairs whose languages differ — a
    # KR / EN headline pair would always score 0 and waste cycles, but
    # also we don't *want* to dedup across languages anyway.
    for a in range(len(signatures)):
        i_a, sig_a, it_a = signatures[a]
        if len(sig_a) < MIN_SIGNATURE_TOKENS:
            continue
        lang_a = it_a.get("lang") or "en"
        for b in range(a + 1, len(signatures)):
            i_b, sig_b, it_b = signatures[b]
            if (it_b.get("lang") or "en") != lang_a:
                continue
            if len(sig_b) < MIN_SIGNATURE_TOKENS:
                continue
            inter = sig_a & sig_b
            if not inter:
                continue
            # Jaccard = |A ∩ B| / |A ∪ B|
            union_size = len(sig_a | sig_b)
            if union_size == 0:
                continue
            jaccard = len(inter) / union_size
            if jaccard >= SIMILARITY_THRESHOLD:
                union(i_a, i_b)

    # Bucket items by cluster root.
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(items)):
        clusters[find(i)].append(i)

    kept: list[dict] = []
    removed = 0
    multi_clusters = 0
    singles = 0
    for root, idxs in clusters.items():
        if len(idxs) == 1:
            kept.append(items[idxs[0]])
            singles += 1
            continue
        multi_clusters += 1
        # Sort cluster members by quality, take top N.
        idxs.sort(key=lambda i: _quality_rank(items[i]), reverse=True)
        survivors = idxs[:MAX_PER_CLUSTER]
        for i in survivors:
            kept.append(items[i])
        removed += len(idxs) - len(survivors)

    # Restore original order among survivors so the curator's ranking
    # is preserved within the deduped set.
    kept_urls = {it.get("url") for it in kept}
    ordered = [it for it in items if it.get("url") in kept_urls]
    return ordered, {
        "clusters": multi_clusters,
        "removed": removed,
        "kept_singles": singles,
    }

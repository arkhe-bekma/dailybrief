"""Smart sorter agent.

Takes the curator's ranked output and produces a balanced feed:

  - Per-category QUOTAs so no single category drowns the page.
  - Per-outlet caps inside each category so one outlet can't take all
    the slots for, say, Korea.
  - Weight multiplier from `config.outlet_meta(...)` — premium outlets
    float up, low-weight outlets get pushed down.
  - A "quality" score 0–100 derived from outlet weight + body length +
    image presence, persisted to the DB for the lab dashboard.
  - Top-up: if a category falls short of its quota (RSS just didn't have
    enough today), we re-fill from the leftover pool until the page hits
    its target total or the pool runs out.

The shape is plain dicts (the same shape curator.rank returns) — this
runs after the curator and before mixer.from_news, so downstream code
doesn't need to change.
"""

from __future__ import annotations

from collections import defaultdict

from backend import config


# How many slots each category gets BEFORE top-up. These are the
# "controlled, concise numbers" the user asked for. Total adds up to
# roughly TOP_K so the page is dense without one category swamping it.
CATEGORY_QUOTAS: dict[str, int] = {
    "world":   28,
    "econ":    26,
    "biz":     16,
    "tech":    24,
    "ai":      20,
    "crypto":  16,
    "science": 14,
    "geo":     14,
    "opinion": 18,
    "korea":   40,
    "kent":    24,
}

# Hard cap on how many items a single outlet can contribute to a
# category. Stops "조선일보 spammed 30 stories" outcomes inside one chip.
PER_OUTLET_CAP = 2

# Global cap on how many items a single outlet can take across the
# entire feed (sum across all categories). Without this, an outlet that
# publishes in many categories (Yonhap, Hankyoreh, Reuters) can quietly
# take 4×N slots = 30+ cards. Hard ceiling — applied after the per-
# category cap.
GLOBAL_PER_OUTLET_CAP = 3


def _quality_score(item: dict) -> float:
    """Heuristic 0–100. Combines outlet weight, body length, image
    presence and the curator's own score. Kept dependency-free so it
    runs in the hot path without round-tripping to the LLM."""
    meta = config.outlet_meta(item.get("outlet") or "")
    weight = float(meta["weight"])
    base = float(item.get("score") or 0)
    has_image = 1.0 if item.get("image") else 0.0
    body_len = len((item.get("summary") or item.get("dek") or "").strip())
    body_bonus = min(20.0, body_len / 12.0)   # 240 chars → +20
    image_bonus = 12.0 * has_image
    # Weight scales the curator score modestly; bonuses are additive.
    out = base * weight * 0.7 + body_bonus + image_bonus
    if meta["premium"]:
        out += 8.0
    return max(0.0, min(100.0, out))


def annotate(items: list[dict]) -> list[dict]:
    """Mutate items with `premium`, `weight`, `quality` from outlet meta.
    Idempotent — safe to call twice."""
    for it in items:
        meta = config.outlet_meta(it.get("outlet") or "")
        it["premium"] = bool(meta["premium"])
        it["weight"] = float(meta["weight"])
        it["quality"] = round(_quality_score(it), 1)
    return items


def balance(
    items: list[dict],
    target_total: int | None = None,
    quotas: dict[str, int] | None = None,
    per_outlet_cap: int = PER_OUTLET_CAP,
) -> list[dict]:
    """Return a re-ordered slice of `items` honouring quotas, per-outlet
    caps, and top-up. The slice is sorted globally by quality so the
    HERO at index 0 is the best of the best.
    """
    annotate(items)
    qmap = dict(CATEGORY_QUOTAS)
    if quotas:
        qmap.update(quotas)
    if target_total is None:
        target_total = sum(qmap.values())

    # Sort once globally by quality so the per-category picks are taken
    # in best-first order.
    sorted_items = sorted(items, key=lambda x: x.get("quality", 0), reverse=True)

    # Pass 1: fill each category up to its quota, respecting per-outlet
    # cap (per category) AND a global per-outlet cap so a single outlet
    # can't dominate the entire feed by publishing across many chips.
    chosen: list[dict] = []
    cat_count: dict[str, int] = defaultdict(int)
    outlet_count_in_cat: dict[tuple[str, str], int] = defaultdict(int)
    outlet_count_global: dict[str, int] = defaultdict(int)
    used_urls: set[str] = set()
    leftover: list[dict] = []

    for it in sorted_items:
        url = it.get("url") or ""
        if url in used_urls:
            continue
        cat = it.get("category") or "world"
        outlet = it.get("outlet") or ""
        quota = qmap.get(cat, 12)
        if cat_count[cat] >= quota:
            leftover.append(it)
            continue
        if outlet_count_in_cat[(cat, outlet)] >= per_outlet_cap:
            leftover.append(it)
            continue
        if outlet and outlet_count_global[outlet] >= GLOBAL_PER_OUTLET_CAP:
            leftover.append(it)
            continue
        chosen.append(it)
        used_urls.add(url)
        cat_count[cat] += 1
        outlet_count_in_cat[(cat, outlet)] += 1
        outlet_count_global[outlet] += 1

    # Pass 2: top-up from leftover. Any category that fell short of its
    # quota AFTER pass 1 gets refilled — but the GLOBAL per-outlet cap
    # still applies (the per-category cap is the only one that relaxes,
    # so we'd rather have a story than a hole *inside* a chip). Without
    # this the dominant outlet would refill all the empty chips too.
    if len(chosen) < target_total and leftover:
        deficit_cats = [c for c, q in qmap.items() if cat_count[c] < q]
        deficit_cats.sort(key=lambda c: qmap[c] - cat_count[c], reverse=True)
        for it in list(leftover):
            if len(chosen) >= target_total:
                break
            cat = it.get("category") or "world"
            if cat not in deficit_cats:
                continue
            if cat_count[cat] >= qmap.get(cat, 12):
                continue
            outlet = it.get("outlet") or ""
            if outlet and outlet_count_global[outlet] >= GLOBAL_PER_OUTLET_CAP:
                continue
            url = it.get("url") or ""
            if url in used_urls:
                continue
            chosen.append(it)
            used_urls.add(url)
            cat_count[cat] += 1
            outlet_count_global[outlet] += 1
            leftover.remove(it)

    # Pass 3: if we still have room, pull the best leftover regardless
    # of category — global per-outlet cap still applies. Caps the page
    # at target_total.
    if len(chosen) < target_total:
        for it in leftover:
            if len(chosen) >= target_total:
                break
            outlet = it.get("outlet") or ""
            if outlet and outlet_count_global[outlet] >= GLOBAL_PER_OUTLET_CAP:
                continue
            url = it.get("url") or ""
            if url in used_urls:
                continue
            chosen.append(it)
            used_urls.add(url)
            outlet_count_global[outlet] += 1

    # Final global re-sort by quality so HERO position is meaningful.
    chosen.sort(key=lambda x: x.get("quality", 0), reverse=True)
    return chosen

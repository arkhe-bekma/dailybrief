"""Quality validator — the gate every article passes through before
it's allowed to show up on the feed.

The user's rule: an article must be "presentable as premium" before
the UI sees it. Title + a real body + an image, no paywall stubs, no
empty shells. Anything that fails gets marked `validated = -1` with a
machine-readable reason; anything that passes gets `validated = 1`
and its body persisted to reader_results so a future publisher URL
death can't lose the content.

This module is intentionally pure-functional: it takes the article
metadata + a body payload and returns (passed, reason). All I/O
(reader.extract, db writes) lives in the worker that calls it.
"""

from __future__ import annotations

import re


# ── Heuristics ─────────────────────────────────────────────────────
MIN_PARAGRAPHS = 2
MIN_BODY_CHARS = 220        # ≈ 50 English words, ~80 Korean chars
MIN_TITLE_CHARS = 10
MAX_PAYWALL_SCAN = 600      # chars at the start of body to scan for stub markers


# Paywall / consent-wall / interstitial signals. Conservative — false
# positives cost us a card but false negatives leak paywall ghosts onto
# the feed which is worse for "premium feel".
_PAYWALL_PATTERNS = (
    "subscribe to continue",
    "sign in to continue",
    "subscribe to read",
    "this article is for subscribers",
    "create a free account to read",
    "you have reached your limit",
    "register for free to read",
    "log in to read",
    "to continue reading",
    "subscriber-only",
    "subscribers only",
    "premium content",
    "을(를) 보려면 구독",
    "구독자만 볼 수 있",
    "로그인 후 이용",
    "회원 가입 후",
    "프리미엄 회원",
    # Cloudflare / consent walls
    "enable javascript",
    "please enable cookies",
    "checking your browser",
)


_LOGO_HINT_RE = re.compile(
    r"(?:logo|brand|favicon|share-card|og-default|placeholder)",
    re.IGNORECASE,
)


def _is_paywall_stub(body_text: str) -> bool:
    if not body_text:
        return False
    head = body_text[:MAX_PAYWALL_SCAN].lower()
    return any(p in head for p in _PAYWALL_PATTERNS)


def _looks_like_logo(image_url: str | None) -> bool:
    """Reject the publisher's brand share-card — those count as
    "image-less" for validation purposes."""
    if not image_url:
        return True
    return bool(_LOGO_HINT_RE.search(image_url))


def validate(
    article: dict, body_payload: dict | None,
) -> tuple[bool, str]:
    """Returns (passes, reason). `reason` is a short stable slug like
    "ok" / "body-too-short" / "paywall" — surfaced in the lab so the
    user can see at a glance why each rejected article got rejected.

    `article` is the article dict (with title, image, outlet, etc.)
    `body_payload` is what reader.extract / db.get_reader returned —
    a dict with `paragraphs` (list[str]), `image`, `title`. None
    means extraction failed entirely.
    """
    title = (article.get("title") or "").strip()
    if len(title) < MIN_TITLE_CHARS:
        return False, "title-too-short"

    if not body_payload:
        return False, "no-body"

    paras = [p.strip() for p in (body_payload.get("paragraphs") or []) if p and p.strip()]
    body_text = "\n".join(paras)

    if len(paras) < MIN_PARAGRAPHS:
        return False, "too-few-paragraphs"
    if len(body_text) < MIN_BODY_CHARS:
        return False, "body-too-short"
    if _is_paywall_stub(body_text):
        return False, "paywall"

    # Image rule: either the article has a usable image, OR the body
    # is long enough that the card can stand on its own as a text card.
    image = (article.get("image")
             or body_payload.get("image")
             or "")
    has_real_image = bool(image) and not _looks_like_logo(image)
    text_self_supporting = len(body_text) >= 600

    if not has_real_image and not text_self_supporting:
        return False, "no-image-and-short"

    return True, "ok"

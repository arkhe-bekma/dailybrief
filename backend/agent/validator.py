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
# Tightened 2026-06: a Korean breaking-news stub ("속보입니다. 업데이트를
# 위해 새로고침 해주십시오." after one short lede) and a 3-line Intel
# article both leaked through the old 400-char / 2-paragraph gate and
# ended up on the feed top. Raising the bar means we drop more rows
# but the survivors actually feel like premium articles.
MIN_PARAGRAPHS = 4          # was 2 — needs real lede + body + context
MIN_BODY_CHARS = 1200       # ≈ 240 EN words / 400 KO chars
MIN_TITLE_CHARS = 10
MAX_PAYWALL_SCAN = 600
MAX_TITLE_REPEATS_IN_BODY = 1   # >1 means the body just echoes the title

# Premium tier: bumped requirement so an article promoted to the
# hero/feature slots can't be a 4-paragraph stub.
PREMIUM_MIN_BODY_CHARS = 1800
PREMIUM_MIN_PARAGRAPHS = 5


# Placeholder / live-update markers. If the body ends with one of
# these, the article is a stub waiting for a real update — drop it.
# Patterns are CASE-INSENSITIVE substrings (Korean stays case-sensitive
# since Hangul has no case).
_LIVE_UPDATE_PATTERNS = (
    # Korean
    "업데이트를 위해 새로고침",
    "추후 업데이트",
    "추후 업데이트됩니다",
    "기사 업데이트 예정",
    "내용 추가",
    "추가될 예정",
    "더 자세한 내용은 추후",
    # English
    "this story is developing",
    "this is a developing story",
    "developing story",
    "this story is being updated",
    "more to come",
    "story will be updated",
    "this article will be updated",
    "refresh for updates",
    "check back for updates",
    "more details to come",
    "stay tuned for updates",
    "this is a breaking news story",
    "breaking news, please check back",
)


# Paywall / consent-wall / interstitial signals.
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


# Copyright / "you may not redistribute" blocks publishers append to
# short articles. When the body is mostly THIS, it's not really news.
_COPYRIGHT_PATTERNS = (
    "all rights reserved",
    "저작권법의 보호",
    "무단 전재",
    "무단 복제",
    "ai 학습 활용",
    "민형사상 법적 책임",
    "사전 허가 없는",
    "재배포 금지",
    "Copyright ©",
    "저작권자",
)


# Series-filler markers (already filtered at the rss layer, but the
# validator catches anything that leaked through with a different
# trafilatura title than the RSS one).
_SERIES_FILLER_TITLE_RE = re.compile(
    r"^\s*(?:\[(?:포토|영상|사진|화보|갤러리|Photos?|Gallery|Slideshow|Watch|Video)[^\]]*\]"
    r"|(?:Photos?|Gallery|Watch|Video|Slideshow)\s*:)",
    re.IGNORECASE,
)


# Generic page-furniture labels trafilatura sometimes mistakes for the
# article title (sidebar headings, "Editor's choice" widget labels,
# section nav, error pages). Anything that ONLY matches one of these
# gets rejected so the feed can never show a card with a meaningless
# headline. Mirrors main._BAD_TITLE_RE but lives here too so the
# validator stays self-contained.
_BOILERPLATE_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"editor[''’]?s?\s+choice"
    r"|editor[''’]?s?\s+pick(?:s)?"
    r"|latest\s+news"
    r"|breaking\s+news"
    r"|top\s+stor(?:y|ies)"
    r"|most\s+(?:read|popular|viewed)"
    r"|more\s+from\b.*"
    r"|recommended\s+(?:reads?|for\s+you)"
    r"|trending(?:\s+now)?"
    r"|featured(?:\s+stor(?:y|ies))?"
    r"|popular\s+(?:now|today|stories)"
    r"|today[''’]?s?\s+(?:pick|top)\w*"
    r"|news(?:letter)?$"
    r"|home\s*$"
    r"|404\b.*"
    r"|page\s+not\s+found"
    r"|access\s+denied"
    r"|sign\s+in\b.*"
    r"|log\s+in\b.*"
    r"|편집자\s*추천"
    r"|많이\s*본\s*뉴스"
    r"|주요\s*뉴스"
    r"|최신\s*뉴스"
    r"|인기\s*기사"
    r")\s*$",
    re.IGNORECASE,
)


def _is_mostly_copyright(body_text: str) -> bool:
    """True when ≥ 30% of the body text matches copyright/disclaimer
    boilerplate. Catches the 한경 "프리미엄9의 모든 콘텐츠는…" tail
    that was making 100-word stubs look like full articles."""
    if not body_text:
        return False
    total = len(body_text)
    if total < 200:
        return False
    matched = 0
    lower = body_text.lower()
    for needle in _COPYRIGHT_PATTERNS:
        n = needle.lower()
        idx = 0
        while True:
            i = lower.find(n, idx)
            if i < 0:
                break
            matched += len(needle)
            idx = i + len(needle)
    return matched > 0 and (matched / total) >= 0.25


_LOGO_HINT_RE = re.compile(
    r"(?:logo|brand|favicon|share-card|og-default|placeholder)",
    re.IGNORECASE,
)


def _is_paywall_stub(body_text: str) -> bool:
    if not body_text:
        return False
    head = body_text[:MAX_PAYWALL_SCAN].lower()
    return any(p in head for p in _PAYWALL_PATTERNS)


def _is_live_update_stub(body_text: str, paragraphs: list[str]) -> bool:
    """True when the article is a placeholder waiting to be filled in
    (속보 with 'refresh for updates', 'this is a developing story', etc.)
    Match anywhere in the body — these phrases routinely show up in the
    LAST paragraph but Korean wire copy sometimes leads with them too."""
    if not body_text:
        return False
    lower = body_text.lower()
    for needle in _LIVE_UPDATE_PATTERNS:
        if needle.lower() in lower:
            return True
    # Hyper-short Korean breaking-news pattern: "속보" or "[속보]" anywhere
    # combined with a body < 600 chars. This catches outlets that put
    # 속보 in the body itself rather than as a placeholder tail.
    if len(body_text) < 600:
        for marker in ("[속보]", "속보:", "[BREAKING]", "BREAKING:"):
            if marker in body_text or marker.lower() in lower:
                return True
    return False


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
    if _SERIES_FILLER_TITLE_RE.match(title):
        return False, "series-filler"
    if _BOILERPLATE_TITLE_RE.match(title):
        return False, "boilerplate-title"

    if not body_payload:
        return False, "no-body"

    # Trafilatura's extracted title — when it disagrees with the RSS
    # title and matches a known sidebar label, the page handed us a
    # widget header instead of the article. Reject so a user clicking
    # the card never lands on a reader modal headlined "Editor's choice".
    extracted_title = (body_payload.get("title") or "").strip()
    if extracted_title and _BOILERPLATE_TITLE_RE.match(extracted_title):
        return False, "boilerplate-title-extracted"

    raw_paras = [p.strip() for p in (body_payload.get("paragraphs") or []) if p and p.strip()]

    # Dedup paragraphs (case-insensitive). Some publishers / trafilatura
    # extracts repeat the same line — Al Jazeera's 67-word example had
    # the title literally 3 times as separate paragraphs.
    seen: set[str] = set()
    paras: list[str] = []
    title_norm = title.lower().strip()
    title_repeats = 0
    for p in raw_paras:
        norm = p.lower().strip()
        # Drop body paragraphs that ARE the article title.
        if title_norm and (norm == title_norm or norm.startswith(title_norm) and len(norm) <= len(title_norm) + 4):
            title_repeats += 1
            continue
        if norm in seen:
            continue
        seen.add(norm)
        paras.append(p)

    if title_repeats > MAX_TITLE_REPEATS_IN_BODY:
        return False, "title-echoed-in-body"

    body_text = "\n".join(paras)

    if len(paras) < MIN_PARAGRAPHS:
        return False, "too-few-paragraphs"
    if len(body_text) < MIN_BODY_CHARS:
        return False, "body-too-short"
    if _is_paywall_stub(body_text):
        return False, "paywall"
    if _is_mostly_copyright(body_text):
        return False, "mostly-copyright"
    if _is_live_update_stub(body_text, paras):
        return False, "live-update-stub"

    # Image rule: either the article has a usable image, OR the body
    # is long enough that the card can stand on its own as a text card.
    image = (article.get("image")
             or body_payload.get("image")
             or "")
    has_real_image = bool(image) and not _looks_like_logo(image)
    # Pumped from 600 → 1400 so the text-only fallback only fires for
    # articles that really do have substance. A 600-char text-only card
    # was usually a wire-service stub.
    text_self_supporting = len(body_text) >= 1400

    if not has_real_image and not text_self_supporting:
        return False, "no-image-and-short"

    # Tier downgrade: premium outlets (NYT, FT, Bloomberg, etc.) can
    # still ship short hits. If the body is below PREMIUM_MIN, strip
    # the premium flag so the sorter doesn't promote it to a hero slot
    # — the article still appears on the wall, just in the regular pool.
    if article.get("premium"):
        if (len(body_text) < PREMIUM_MIN_BODY_CHARS
                or len(paras) < PREMIUM_MIN_PARAGRAPHS):
            article["premium"] = False
            article["weight"] = min(1.0, float(article.get("weight") or 1.0))

    return True, "ok"

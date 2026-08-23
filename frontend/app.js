const $ = (s) => document.querySelector(s);

// ── Formatters ─────────────────────────────────────────────────
const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)        return "now";
  if (diff < 3600)      return `${Math.floor(diff / 60)}m`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};
const fmtUSD = (n) => {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n}`;
};
const fmtPrice = (p) => {
  if (p >= 10000) return p.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (p >= 1)     return p.toFixed(2);
  return p.toFixed(4);
};
const fmtPct = (p) => `${p >= 0 ? "+" : ""}${p.toFixed(2)}%`;
const stripHtml = (s) => (s || "").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();

// Decode HTML entities like &amp; → & and &#39; → ' so they never reach
// the title on screen. Defensive — backend already runs html.unescape
// but some publisher RSS feeds double-escape and we don't want any of
// that to leak.
const _ent = document.createElement("textarea");
function decodeEntities(s) {
  if (!s) return "";
  _ent.innerHTML = s;
  return _ent.value;
}

// Formal "DD MON YYYY H:MM AM/PM" — used in the reader modal next to
// the word count. Example: "14 MAY 2026 7:13 PM".
function fmtFormal(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const day = d.getDate();
  const month = months[d.getMonth()];
  const year = d.getFullYear();
  let hour = d.getHours();
  const ampm = hour >= 12 ? "PM" : "AM";
  hour = hour % 12 || 12;
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${day} ${month} ${year} ${hour}:${min} ${ampm}`;
}

// More natural "when" display, mixed Korean/English to match the
// reader's primary language.  Format:
//   - Same day:  "오늘 14:32"
//   - Yesterday: "어제 09:11"
//   - This year: "5월 28일 14:32"  /  "May 28 14:32"
//   - Older:     "2024.05.28"      /  "May 28, 2024"
function fmtPub(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  const isYest = d.toDateString() === yest.toDateString();
  const sameYear = d.getFullYear() === now.getFullYear();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  // Detect Korean preference from the page lang or the brief profile.
  const lang =
    (STATE.profile && STATE.profile.primary_lang) ||
    document.documentElement.lang ||
    "en";
  const ko = lang === "ko";
  if (sameDay)  return ko ? `오늘 ${hh}:${mm}` : `TODAY ${hh}:${mm}`;
  if (isYest)   return ko ? `어제 ${hh}:${mm}` : `YEST ${hh}:${mm}`;
  if (sameYear) {
    return ko
      ? `${d.getMonth() + 1}월 ${d.getDate()}일 ${hh}:${mm}`
      : d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ` ${hh}:${mm}`;
  }
  return ko
    ? `${d.getFullYear()}.${String(d.getMonth()+1).padStart(2,"0")}.${String(d.getDate()).padStart(2,"0")}`
    : d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

// Backend already merges real + AI URLs into item.image.
const pickImage = (item) => item.image || null;

const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (v == null) return;
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else node.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children]).forEach((c) => {
    if (c == null) return;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
};

// ── Sparkline SVG ──────────────────────────────────────────────
function sparkSVG(values, w = 48, h = 16, color = null) {
  if (!values || values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const stepX = w / (values.length - 1);
  const pts = values.map((v, i) => {
    const x = i * stepX;
    const y = h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const up = values[values.length - 1] >= values[0];
  const stroke = color || (up ? "var(--up)" : "var(--down)");
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const poly = document.createElementNS(NS, "polyline");
  poly.setAttribute("points", pts);
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", stroke);
  poly.setAttribute("stroke-width", "1.6");
  poly.setAttribute("stroke-linejoin", "round");
  poly.setAttribute("stroke-linecap", "round");
  svg.appendChild(poly);
  return svg;
}

// ── Ticker tape ────────────────────────────────────────────────
function renderTape(quotes) {
  const tape = $("#tape");
  tape.innerHTML = "";
  if (!quotes || !quotes.length) return;
  const track = document.createElement("div");
  track.className = "tape-track";
  const buildBlock = () => {
    const frag = document.createDocumentFragment();
    quotes.forEach((q) => {
      const up = q.change_pct >= 0;
      const tick = el("span", { class: "tick" }, [
        el("span", { class: "lbl" }, q.label),
        el("span", { class: "px" }, fmtPrice(q.price)),
        el("span", { class: `ch ${up ? "up" : "down"}` }, fmtPct(q.change_pct)),
      ]);
      const sv = sparkSVG(q.spark, 44, 14);
      if (sv) tick.appendChild(sv);
      frag.appendChild(tick);
    });
    return frag;
  };
  track.appendChild(buildBlock());
  track.appendChild(buildBlock());
  tape.appendChild(track);
}

// ── News card body ─────────────────────────────────────────────
function newsBody(item, opts = {}) {
  // When the card has a stored translation and the user has toggled
  // it on (CARD_KO_URLS), swap title + dek for the Korean version.
  const koView = !!opts.koView && !!item.title_ko;
  const lang = koView ? "ko" : (item.lang || "en");
  const titleText = koView && item.title_ko ? item.title_ko : (item.title || "");
  const dekText   = koView && item.dek_ko   ? item.dek_ko   : (item.dek || "");
  const meta = el("div", { class: "meta" }, [
    el("span", { class: "tag" }, (item.category || "news").toUpperCase()),
    el("span", { class: "src" }, item.outlet || ""),
    item.premium ? el("span", { class: "premium-pip", title: "premium outlet" }, "★ PREMIUM") : null,
    el("span", { class: `lang lang-${lang}` }, lang.toUpperCase()),
    item.score != null ? el("span", { class: "score-pill" }, `★${item.score}`) : null,
  ]);
  const head = el("h2", { class: "h", lang }, decodeEntities(titleText) || "");
  const dek = dekText ? el("p", { class: "dek", lang }, decodeEntities(dekText)) : null;
  // .why was an AI-generated 1-line "why this matters" from the
  // curator. User saw it as confusing AI-shortened content next to
  // the dek. Removed from card layout — the actual extracted body
  // is what appears under the title now, full stop. No AI rewrites.
  const why = null;
  const sparks = item.sparks || {};
  const tickers = (item.tickers || []).filter((t) => sparks[t]);
  let sparkRow = null;
  if (tickers.length) {
    sparkRow = el("div", { class: "spark-row" }, tickers.map((t) => {
      const v = sparks[t];
      const sv = sparkSVG(v);
      const pct = ((v[v.length - 1] - v[0]) / v[0]) * 100;
      const up = pct >= 0;
      return el("span", { class: "spark" }, [
        el("span", { class: "sym" }, t.replace("-USD", "").replace(".KS", "")),
        sv,
        el("span", { class: `ch ${up ? "up" : "down"}` }, fmtPct(pct)),
      ]);
    }));
  }
  return { meta, head, dek, why, sparkRow };
}

// Tracks which article cards are currently rendered in their Korean
// translated view. Persisted to localStorage so chip nav / pagination
// preserve the toggle across re-paints.
const CARD_KO_KEY = "dailybrief.cardKo.v1";
let CARD_KO_URLS = new Set();
try {
  const raw = localStorage.getItem(CARD_KO_KEY);
  if (raw) CARD_KO_URLS = new Set(JSON.parse(raw));
} catch {}
function _persistCardKo() {
  try { localStorage.setItem(CARD_KO_KEY, JSON.stringify([...CARD_KO_URLS])); } catch {}
}

// In-place swap of a single card node. Beats a full paint() because
// only one card flickers; pagination + scroll stay put.
function rerenderCard(url) {
  if (!url) return false;
  const oldNode = [...document.querySelectorAll(".art")]
    .find((n) => n.getAttribute("data-url") === url);
  if (!oldNode) return false;
  const item =
    (STATE.mixed || []).find((m) => m.url === url) ||
    (PAGE_OVERRIDE || []).find((m) => m.url === url);
  if (!item) return false;
  const tierMatch = oldNode.className.match(/\btier-(\w+)\b/);
  const tier = tierMatch ? tierMatch[1] : "small";
  const fresh = renderNewsCard(item, tier);
  fresh.classList.add("no-fade");
  oldNode.replaceWith(fresh);
  return true;
}

function renderNewsCard(item, tier) {
  const inKo = !!item.title_ko && CARD_KO_URLS.has(item.url);
  const parts = newsBody(item, { koView: inKo });
  const cat = item.category || "world";
  const klass = [`art`, `tier-${tier}`, `cat-${cat}`];
  if (item.title_ko) klass.push("has-ko");
  if (inKo) klass.push("ko-on");
  const node = el("a", {
    class: klass.join(" "),
    href: item.url || "#",
    target: "_blank",
    rel: "noopener",
    "data-url": item.url || "",
  });

  const wantsImage = ["hero", "feature", "large", "medium", "small"].includes(tier);
  const imgUrl = wantsImage ? pickImage(item) : null;

  // ✦한 / 원문 toggle badge on the image — appears only when a stored
  // translation exists for this URL. Click swaps the card preview
  // between English (source) and Korean inline without opening the
  // modal. Persists per-URL in CARD_KO_URLS.
  let koBadge = null;
  if (false && item.title_ko) {
    koBadge = el("button", {
      class: "ko-badge",
      type: "button",
      "data-ko-toggle": "1",
      title: inKo ? "원문 보기" : "한국어 미리보기",
      "aria-label": "translation toggle",
    }, [
      el("span", { class: "kb-glyph", "aria-hidden": "true" }, "✦"),
      el("span", { class: "kb-text", lang: inKo ? "en" : "ko" }, inKo ? "EN" : "한"),
    ]);
  }

  // × delete button — admin-only (CSS hides for non-admins).
  const delBtn = el("button", {
    class: "card-delete",
    type: "button",
    "data-delete": "1",
    title: "remove this article",
    "aria-label": "remove article",
  }, "×");

  if (imgUrl) {
    const imgWrap = el("div", { class: "img-wrap" });
    const imgEl = el("div", { class: "img", style: `background-image:url('${imgUrl}')` });
    // A background-image that 404s or gets hotlink-blocked fails
    // silently — the card just renders an empty dark slab. Probe the URL
    // and mark the element so CSS can fall back to the category tint,
    // which reads as a deliberate placeholder rather than a bug.
    const probe = new Image();
    probe.onerror = () => imgEl.classList.add("img-failed");
    probe.src = imgUrl;
    imgWrap.appendChild(imgEl);
    if (koBadge) imgWrap.appendChild(koBadge);
    imgWrap.appendChild(delBtn);
    if (tier === "hero") {
      node.appendChild(imgWrap);
      node.appendChild(el("div", { class: "body" }, [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow]));
    } else {
      node.appendChild(imgWrap);
      [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    }
  } else {
    [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    // No image? Drop the toggle badge into the meta line so it's still
    // accessible on text-only cards.
    if (koBadge) {
      koBadge.classList.add("ko-badge-inline");
      parts.meta.appendChild(koBadge);
    }
    delBtn.classList.add("card-delete-inline");
    node.appendChild(delBtn);
  }
  return node;
}

// ── Score-based tier assignment ────────────────────────────────
// Image-bearing items (real photo or backend AI-image) fill HERO →
// FEATURE → LARGE → MEDIUM → SMALL. Items that are missing both fall
// to HEADLINE/FLASH text tiers, which live in the float section.
function buildPage(news) {
  // New policy:
  //   - Hero = single highest-scored article that has an image (the bot
  //     picks the most popular).
  //   - Body = first article from each outlet, in descending score order.
  //     One article per outlet, no duplicate URLs anywhere on the page.
  //   - Anything left over (after every outlet got a turn) tails the
  //     page in score order so the wall doesnt shrink to nothing on
  //     low-source days.
  const sorted = [...news].sort((a, b) => b.score - a.score);
  const seenUrls = new Set();
  const seenOutlets = new Set();
  const out = [];
  const budget = { feature: 2, large: 4, medium: 8 };

  // 1) hero — highest-scored with an image
  let heroIdx = -1;
  for (let i = 0; i < sorted.length; i++) {
    if (pickImage(sorted[i])) { heroIdx = i; break; }
  }
  if (heroIdx >= 0) {
    const it = sorted[heroIdx];
    out.push({ item: it, tier: "hero" });
    seenUrls.add(it.url);
    seenOutlets.add(it.outlet);
  }

  // 2) body — one per outlet, in score order
  for (const item of sorted) {
    if (seenUrls.has(item.url)) continue;
    if (seenOutlets.has(item.outlet)) continue;
    seenUrls.add(item.url);
    seenOutlets.add(item.outlet);
    const hasImage = !!pickImage(item);
    let tier;
    if (hasImage) {
      if (budget.feature > 0)     { tier = "feature"; budget.feature--; }
      else if (budget.large > 0)  { tier = "large";   budget.large--; }
      else if (budget.medium > 0) { tier = "medium";  budget.medium--; }
      else                        { tier = "small"; }
    } else {
      tier = item.score >= 70 ? "headline" : "flash";
    }
    out.push({ item, tier });
  }

  // 3) fallback tail — fill with leftover articles so we never serve a
  //    half-empty wall on days when only a handful of outlets reported.
  for (const item of sorted) {
    if (seenUrls.has(item.url)) continue;
    seenUrls.add(item.url);
    const hasImage = !!pickImage(item);
    out.push({ item, tier: hasImage ? "small" : (item.score >= 70 ? "headline" : "flash") });
  }

  return out;
}

// ── State ──────────────────────────────────────────────────────
let STATE = { mixed: [], tape: [], whales: [], trades: [], youtube: [] };
let CAT = "all";
let PAGE = 1;
let LAST_LOAD = null;
let PENDING_REFRESH = false;  // set true when silentRefresh is deferred
// Smaller page size means fewer total pages even with thousands of
// articles in the archive. The user explicitly wanted a "premium
// committee" feel — one tidy page at a time, not a 158-page wall.
// 13 = HERO (1) + 12 of the rest, which packs cleanly into the
// 2-per-row tiers below the hero.
// User-tuned: 20 cards per page. 6 felt too sparse, 30 produced single-
// page-only states when the body-first gate trimmed the pool. 20 is
// the sweet spot — 37 validated articles → 2 pages, 60 → 3, etc.
const PAGE_SIZE = 20;
const AUTO_REFRESH_MS = 2 * 60 * 1000;  // 2 minutes — near-real-time

// User-tunable interests — categories the user wants boosted in the
// "all" view. Stored as a Set in localStorage. When non-empty, items
// in those categories sort to the top of the all-feed; the per-item
// curator score still determines order WITHIN each bucket.
const INTERESTS_KEY = "dailybrief.interests.v1";
let INTERESTS = new Set();
try {
  const raw = localStorage.getItem(INTERESTS_KEY);
  if (raw) INTERESTS = new Set(JSON.parse(raw));
} catch {}
function _persistInterests() {
  try { localStorage.setItem(INTERESTS_KEY, JSON.stringify([...INTERESTS])); } catch {}
}

function filteredNews() {
  const items = STATE.mixed.filter((it) => {
    if (it.kind !== "news") return false;
    if (CAT !== "all" && it.category !== CAT) return false;
    return true;
  });
  // No interests configured, or a specific category is selected → leave
  // the curator's ordering alone.
  if (CAT !== "all" || INTERESTS.size === 0) return items;
  // Stable two-bucket sort: interested categories first (preserving
  // curator score within), everyone else after.
  const liked = [];
  const rest = [];
  for (const it of items) {
    if (INTERESTS.has(it.category)) liked.push(it);
    else rest.push(it);
  }
  return [...liked, ...rest];
}

// Lazy DB-backed pagination: page 1 comes from the in-memory feed
// (fast, freshly curated). Pages 2+ are pulled from /api/page so the
// total page count is unbounded — it grows with the archive.
// Items currently painted, in render order. Backs the reader's
// prev/next arrows and its related-stories list.
let RENDERED = [];
// Search mode. When SEARCH_Q is set, paint() renders SEARCH_RESULTS
// instead of the category feed; clearing the query drops straight back
// to whatever category was active, so search is a lens over the feed
// rather than a separate screen to escape from.
let SEARCH_Q = "";
let SEARCH_RESULTS = null;
let SEARCH_TOTAL = 0;
let SEARCH_SEQ = 0;          // guards against out-of-order responses

let PAGE_OVERRIDE = null;   // items[] for the current page when >1
let PAGE_OVERRIDE_N = null; // which page that override belongs to
let DB_TOTAL_PAGES = 1;

async function fetchPage(n) {
  // For "all" page 1 we use the in-memory mixed (fast). Every other
  // combination — page 2+, or any specific category — pulls straight
  // from the SQLite archive. This is what makes the chip nav reach
  // beyond the lean wire payload.
  const fromDb = (n > 1) || (CAT && CAT !== "all");
  if (!fromDb) { PAGE_OVERRIDE = null; PAGE_OVERRIDE_N = null; return; }
  try {
    const params = new URLSearchParams({ n: String(n), size: String(PAGE_SIZE) });
    if (CAT && CAT !== "all" && CAT !== "premium") params.set("cat", CAT);
    if (CAT === "premium") params.set("premium", "1");
    const r = await fetch(`/api/page?${params.toString()}`);
    if (!r.ok) return;
    const d = await r.json();
    PAGE_OVERRIDE = d.items || [];
    PAGE_OVERRIDE_N = d.page;
    DB_TOTAL_PAGES = d.total_pages || 1;
  } catch (e) {
    PAGE_OVERRIDE = null;
    PAGE_OVERRIDE_N = null;
  }
}

function fmtAge(ts) {
  if (!ts) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 60)        return `${diff}s ago`;
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function paint(scrollTop = true) {
  const all = filteredNews();
  const memPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  // Total pages = whatever the DB archive supports, never the cached
  // in-memory window. Always > memPages once articles accumulate.
  const totalPages = Math.max(memPages, DB_TOTAL_PAGES || 1);

  let slice;
  // Search mode short-circuits the whole paging path: results are
  // already the full set we want to show.
  if (SEARCH_Q && SEARCH_RESULTS) {
    slice = SEARCH_RESULTS;
    RENDERED = slice.slice();
    const paperS = $("#paper");
    const paperNoImgS = $("#paper-noimg");
    paperS.innerHTML = "";
    paperNoImgS.innerHTML = "";
    buildPage(slice).forEach(({ item, tier }) => {
      const card = renderNewsCard(item, tier);
      if (tier === "headline" || tier === "flash") paperNoImgS.appendChild(card);
      else paperS.appendChild(card);
    });
    renderSearchMeta();
    $("#pager").innerHTML = "";
    if (scrollTop) window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }

  // Use the DB-served override when present (page 2+, or any category
  // filter). Otherwise use the in-memory mixed slice.
  if (PAGE_OVERRIDE && PAGE_OVERRIDE_N === PAGE) {
    slice = PAGE_OVERRIDE;
  } else {
    if (PAGE > memPages) PAGE = memPages;   // safety while async fetch finishes
    const startIdx = (PAGE - 1) * PAGE_SIZE;
    slice = all.slice(startIdx, startIdx + PAGE_SIZE);
  }

  // Remember the rendered order so the reader can offer prev/next and
  // pull related stories without re-deriving the page layout.
  RENDERED = slice.slice();

  const paper = $("#paper");
  const paperNoImg = $("#paper-noimg");
  paper.innerHTML = "";
  paperNoImg.innerHTML = "";
  buildPage(slice).forEach(({ item, tier }) => {
    const card = renderNewsCard(item, tier);
    // Text-only tiers (no image) go to the float section so they aren't
    // stretched to match neighbours in the grid.
    if (tier === "headline" || tier === "flash") {
      paperNoImg.appendChild(card);
    } else {
      paper.appendChild(card);
    }
  });

  // (Chip counters removed — user said the numbers are noise; the lab
  // dashboard is the source of truth for per-outlet counts.)

  renderPager(totalPages);

  const outlets = STATE.outlets_count
    || Object.keys(STATE.by_outlet || {}).length || 0;
  const totalMixed = STATE.total_mixed || all.length;
  $("#status").textContent =
    `${totalMixed} news · page ${PAGE}/${totalPages} · ${outlets} sources · updated ${fmtAge(LAST_LOAD)}`;
  if (scrollTop) window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderPager(totalPages) {
  const p = $("#pager");
  p.innerHTML = "";
  // The pager is ALWAYS painted — even with one page — so the bottom of
  // the page never looks blank or "missing" on mobile or web. Prev/next
  // disabled at the boundary; the numeric strip collapses to "page X of
  // Y" when there's nowhere to jump to.
  const safeTotal = Math.max(1, totalPages || 1);
  const go = async (n) => {
    PAGE = Math.max(1, Math.min(safeTotal, n));
    await fetchPage(PAGE);
    paint();
  };
  const narrow = window.matchMedia("(max-width: 800px)").matches;
  const WINDOW = narrow ? 3 : 5;
  const windowStart = Math.floor((PAGE - 1) / WINDOW) * WINDOW + 1;
  const windowEnd = Math.min(windowStart + WINDOW - 1, safeTotal);

  // « FIRST — only show when we're past the first window.
  if (windowStart > 1) {
    const first = el("button", { type: "button", class: "pg-arrow", title: "first page" }, "« 1");
    first.onclick = () => go(1);
    p.appendChild(first);
  }

  // ‹ PREV — step back one page.
  const prev = el("button", { type: "button", class: "pg-arrow" }, "‹ PREV");
  prev.disabled = PAGE <= 1;
  prev.onclick = () => go(PAGE - 1);
  p.appendChild(prev);

  if (safeTotal <= 1) {
    p.appendChild(el("span", { class: "label" }, `page ${PAGE} of ${safeTotal}`));
  } else {
    for (let n = windowStart; n <= windowEnd; n++) {
      const btn = el("button", {
        type: "button",
        class: "pg-num" + (n === PAGE ? " pg-active" : ""),
      }, String(n));
      btn.onclick = () => go(n);
      p.appendChild(btn);
    }
    if (windowEnd < safeTotal) {
      const dots = el("span", { class: "pg-dots" }, "…");
      p.appendChild(dots);
    }
  }

  // NEXT › — step forward one page.
  const next = el("button", { type: "button", class: "pg-arrow" }, "NEXT ›");
  next.disabled = PAGE >= safeTotal;
  next.onclick = () => go(PAGE + 1);
  p.appendChild(next);

  // LAST » — quick jump to the final page.
  if (windowEnd < safeTotal) {
    const last = el("button",
      { type: "button", class: "pg-arrow", title: "last page" },
      `${safeTotal} »`);
    last.onclick = () => go(safeTotal);
    p.appendChild(last);
  }
}

// ── Preset cache (localStorage) ────────────────────────────────
// Render the last good /api/brief response immediately on page load
// so a refresh never shows a blank screen while the network call is
// in flight. The freshest payload then swaps in once it arrives.
const PRESET_KEY  = "dailybrief.preset.v1";
const PRESET_MAX  = 1_500_000;   // ~1.5 MB — keeps localStorage happy

function savePreset(state) {
  try {
    const lean = {
      profile: state.profile,
      tape: state.tape,
      headline: state.headline,
      mixed: (state.mixed || []).slice(0, 200),  // cap so we stay under quota
      by_outlet: undefined,                       // unused on the wall
      whales: state.whales,
      trades: state.trades,
      youtube: state.youtube,
      db_total_articles: state.db_total_articles,
      _saved_at: Date.now(),
    };
    const s = JSON.stringify(lean);
    if (s.length <= PRESET_MAX) localStorage.setItem(PRESET_KEY, s);
  } catch (e) { /* quota or serialization issue — non-fatal */ }
}

function loadPreset() {
  try {
    const s = localStorage.getItem(PRESET_KEY);
    if (!s) return null;
    const data = JSON.parse(s);
    if (!data || !Array.isArray(data.mixed)) return null;
    return data;
  } catch (e) { return null; }
}

function paintFromState() {
  renderTape(STATE.tape || []);
  const head = $("#headline");
  head.textContent = STATE.headline || "";
  head.lang = (STATE.profile && STATE.profile.primary_lang) || "en";
  if (typeof STATE.db_total_articles === "number") {
    DB_TOTAL_PAGES = Math.max(
      1, Math.ceil(STATE.db_total_articles / PAGE_SIZE),
    );
  }
  paint(false);
}

// ── Load ───────────────────────────────────────────────────────
async function load() {
  // 1. Hydrate from localStorage preset for instant first paint.
  const cached = loadPreset();
  if (cached) {
    STATE = cached;
    STATE.mixed.forEach((m) => { if (m.dek) m.dek = stripHtml(m.dek); });
    LAST_LOAD = cached._saved_at || Date.now();
    paintFromState();
    $("#status").textContent = "showing cached · refreshing…";
  } else {
    $("#status").textContent = "loading…";
  }

  // 2. Fetch fresh and swap in.
  try {
    const r = await fetch("/api/brief");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    STATE = await r.json();
    STATE.mixed.forEach((m) => { if (m.dek) m.dek = stripHtml(m.dek); });
    LAST_LOAD = Date.now();
    PAGE = 1;
    paintFromState();
    paint(false);     // ensure pager + chip counts redrawn
    savePreset(STATE);
  } catch (e) {
    if (!cached) $("#status").textContent = `error: ${e.message}`;
  }
}

// Silent auto-refresh: pull /api/brief, swap data, redraw current page
// without resetting pagination or scrolling. Triggered on a timer.
async function silentRefresh() {
  // Never reshuffle the grid while the user is reading. Without this
  // guard, the article they're currently on could vanish from the
  // feed during refresh, and closing the modal would land them at a
  // visually-unrelated scroll position. Deferred refresh fires the
  // moment the modal closes via the "pendingRefresh" flag below.
  const readerOpen = !document.getElementById("reader")?.classList.contains("hidden");
  if (readerOpen) {
    PENDING_REFRESH = true;
    return;
  }
  try {
    const r = await fetch("/api/brief");
    if (!r.ok) return;
    const fresh = await r.json();
    fresh.mixed.forEach((m) => { if (m.dek) m.dek = stripHtml(m.dek); });
    STATE = fresh;
    LAST_LOAD = Date.now();
    renderTape(STATE.tape || []);
    const head = $("#headline");
    head.textContent = STATE.headline || "";
    head.lang = (STATE.profile && STATE.profile.primary_lang) || "en";
    if (typeof STATE.db_total_articles === "number") {
      DB_TOTAL_PAGES = Math.max(
        1, Math.ceil(STATE.db_total_articles / PAGE_SIZE),
      );
    }
    renderWhalesStrip(STATE.whales || []);
    renderTradesStrip(STATE.trades || []);
    renderVideosStrip(STATE.youtube || []);
    // ░ Intentionally do NOT paint() the article grid here. The user
    // explicitly asked for "기사 보다가 닫으면 맨위로 가지 말고 그 자리"
    // — repainting the grid even with scrollTop=false can reshuffle
    // article cards under the user (different curator ranking,
    // dedup pulls, etc.) so what they were looking at moves to a
    // different visual position. Tape + headline + strips update so
    // fresh prices and breaking news show, but the article grid stays
    // exactly where they left it until they actively navigate (chip
    // click / pager / explicit refresh).
    const status = $("#status");
    if (status) {
      const outlets = STATE.outlets_count
        || Object.keys(STATE.by_outlet || {}).length || 0;
      const totalMixed = STATE.total_mixed || (STATE.mixed || []).length;
      status.textContent =
        `${totalMixed} news · ${outlets} sources · updated ${fmtAge(LAST_LOAD)}`;
    }
    savePreset(STATE);
  } catch (e) {
    console.warn("auto-refresh failed:", e);
  }
}

// ── Toast ──────────────────────────────────────────────────────
// One-line, self-dismissing notice. Used when an article is pulled out
// from under the user, so the card vanishing is explained rather than
// just happening.
let TOAST_TIMER = null;

function toast(message, ms = 3600) {
  if (!message) return;
  let node = document.getElementById("toast");
  if (!node) {
    node = el("div", { id: "toast", class: "toast", role: "status",
                       "aria-live": "polite" });
    document.body.appendChild(node);
  }
  node.textContent = message;
  // Restart the animation even if a toast is already on screen.
  node.classList.remove("show");
  void node.offsetWidth;
  node.classList.add("show");
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => node.classList.remove("show"), ms);
}


// ── Search ─────────────────────────────────────────────────────
// A lens over the archive rather than a separate page: results reuse
// the card renderer, and clearing the box drops straight back to the
// category the user was already on.

function renderSearchMeta() {
  const meta = $("#search-meta");
  if (!meta) return;
  if (!SEARCH_Q) {
    meta.classList.add("hidden");
    meta.textContent = "";
    return;
  }
  meta.classList.remove("hidden");
  const shown = (SEARCH_RESULTS || []).length;
  if (!shown) {
    meta.textContent = `No results for “${SEARCH_Q}”`;
    return;
  }
  meta.textContent = SEARCH_TOTAL > shown
    ? `${SEARCH_TOTAL} results for “${SEARCH_Q}” · showing first ${shown}`
    : `${shown} result${shown === 1 ? "" : "s"} for “${SEARCH_Q}”`;
}

async function runSearch(q) {
  q = (q || "").trim();
  SEARCH_Q = q;
  const clearBtn = $("#search-clear");
  if (clearBtn) clearBtn.hidden = !q;

  if (q.length < 2) {
    // Too short to be meaningful — drop back to the normal feed rather
    // than showing every article that happens to contain one letter.
    SEARCH_RESULTS = null;
    SEARCH_TOTAL = 0;
    if (!q) { SEARCH_Q = ""; paint(false); }
    renderSearchMeta();
    return;
  }

  const seq = ++SEARCH_SEQ;
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&size=40`);
    const d = await r.json();
    // A slower earlier request must not overwrite a newer one's results.
    if (seq !== SEARCH_SEQ) return;
    SEARCH_RESULTS = d.items || [];
    SEARCH_TOTAL = d.total_items || 0;
    paint(false);
  } catch (e) {
    if (seq !== SEARCH_SEQ) return;
    SEARCH_RESULTS = [];
    SEARCH_TOTAL = 0;
    paint(false);
  }
}

function exitSearch() {
  SEARCH_Q = "";
  SEARCH_RESULTS = null;
  SEARCH_TOTAL = 0;
  const input = $("#search-input");
  if (input) input.value = "";
  const clearBtn = $("#search-clear");
  if (clearBtn) clearBtn.hidden = true;
  renderSearchMeta();
  paint(false);
}

function wireSearch() {
  const bar = $("#searchbar");
  const input = $("#search-input");
  const btn = $("#search-btn");
  const clearBtn = $("#search-clear");
  if (!bar || !input || !btn) return;

  btn.addEventListener("click", () => {
    const opening = bar.classList.contains("hidden");
    bar.classList.toggle("hidden", !opening);
    btn.classList.toggle("on", opening);
    if (opening) input.focus();
    else exitSearch();
  });

  clearBtn?.addEventListener("click", () => {
    exitSearch();
    input.focus();
  });

  // Debounced so typing doesn't fire a query per keystroke.
  let debounce = null;
  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => runSearch(input.value), 220);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      exitSearch();
      bar.classList.add("hidden");
      btn.classList.remove("on");
      input.blur();
    }
    if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(debounce);
      runSearch(input.value);
    }
  });
}


// ── Reader support: reading time, siblings, related ────────────

// Rough read time. Korean is counted by character (Hangul packs far
// more meaning per whitespace-delimited token than English does), so
// word-count alone badly under-estimates Korean articles.
function readingMinutes(paragraphs, lang) {
  const text = (paragraphs || []).join(" ");
  if (!text) return 0;
  if ((lang || "").toLowerCase() === "ko") {
    const chars = text.replace(/\s/g, "").length;
    return Math.max(1, Math.round(chars / 450));
  }
  const words = text.split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / 230));
}

// Where this article sits in the list the user is actually looking at,
// so prev/next follow the visible order rather than some server order.
function readerSiblings(url) {
  const list = (RENDERED && RENDERED.length ? RENDERED : STATE.mixed) || [];
  const i = list.findIndex((m) => m.url === url);
  if (i < 0) return { prev: null, next: null, index: -1, total: list.length };
  return {
    prev: i > 0 ? list[i - 1] : null,
    next: i < list.length - 1 ? list[i + 1] : null,
    index: i,
    total: list.length,
  };
}

// Up to 3 other stories in the same category. Falls back to same outlet
// when the category is thin, and returns nothing rather than padding
// with unrelated items — a bad "related" list is worse than none.
function relatedItems(item, limit = 3) {
  const pool = (STATE.mixed || []).concat(RENDERED || []);
  const seen = new Set([item.url]);
  const unique = pool.filter((m) => {
    if (!m || !m.url || seen.has(m.url)) return false;
    seen.add(m.url);
    return true;
  });
  const sameCat = item.category
    ? unique.filter((m) => m.category === item.category)
    : [];
  if (sameCat.length >= limit) return sameCat.slice(0, limit);
  const sameOutlet = unique.filter(
    (m) => m.outlet && m.outlet === item.outlet && m.category !== item.category,
  );
  return sameCat.concat(sameOutlet).slice(0, limit);
}


// ── Reader modal ───────────────────────────────────────────────
// Holds the article currently open + a cached translation if the
// user has already toggled to the other language during this open.
// On every renderReader the meta strip + the translate button get
// rebuilt fresh; this state just remembers which view to render.
let READER_STATE = {
  url: null,
  item: null,
  original: null,
  translated: null,
  view: "original",   // "original" | "translated"
};

function _targetLang(srcLang) {
  return (srcLang || "en").toLowerCase() === "ko" ? "en" : "ko";
}

async function openReader(url, item) {
  const modal = document.getElementById("reader");
  if (!modal) return;
  modal.classList.remove("hidden");
  // Lock the page behind. The helper captures scrollY BEFORE applying
  // position:fixed — see lockBodyScroll for the iOS scroll-snap bug
  // that used to send the user back to the top on close.
  lockBodyScroll();
  const content = modal.querySelector(".reader-content");
  content.innerHTML = `<div class="reader-loading">📖 READING…</div>`;
  READER_STATE = {
    url, item: item || {},
    original: null, translated: null, view: "original",
  };

  try {
    const r = await fetch(`/api/article?url=${encodeURIComponent(url)}`);
    const data = await r.json();
    // The publisher gave us no body, so the backend took the story out
    // of the feed. Pull the card and get out of the way rather than
    // showing a headline with nothing under it.
    if (data.gone) {
      purgeUrlLocally(url);
      if (RENDERED) RENDERED = RENDERED.filter((m) => m.url !== url);
      if (SEARCH_RESULTS) SEARCH_RESULTS = SEARCH_RESULTS.filter((m) => m.url !== url);
      closeReader();
      toast(data.note || "No article body — removed from the feed.");
      return;
    }
    if (data.error) {
      content.innerHTML =
        `<div class="reader-loading">⚠ ${data.error}<br><br>` +
        `<a class="reader-original" href="${url}" target="_blank" rel="noopener">open original ↗</a></div>`;
      return;
    }
    READER_STATE.original = data;
    renderReader(content, data, item || {});
  } catch (e) {
    content.innerHTML = `<div class="reader-loading">error: ${e.message}</div>`;
  }
}

// Toggle handler attached to the inline translate button (rebuilt in
// renderReader). Logic:
//   - if already in translated view → flip back to original (no fetch)
//   - if we have a cached translation in READER_STATE → just swap views
//   - otherwise fetch /api/translate. The backend is cache-first
//     (in-memory → SQLite reader_results → Gemini) so a translation
//     that was made before by anyone, in either direction, comes back
//     instantly without burning quota.
async function toggleTranslation() {
  if (!READER_STATE.original) return;
  const content = document.querySelector("#reader .reader-content");

  // Cached round-trip.
  if (READER_STATE.view === "translated") {
    READER_STATE.view = "original";
    renderReader(content, READER_STATE.original, READER_STATE.item);
    return;
  }
  if (READER_STATE.translated) {
    READER_STATE.view = "translated";
    renderReader(content, READER_STATE.translated, READER_STATE.item);
    return;
  }

  // First time on this article + direction. Mark loading + fetch.
  const btn = content?.querySelector(".reader-translate");
  if (btn) {
    btn?.classList.add("loading");
    if (btn) btn.disabled = true;
  }
  try {
    const tgt = _targetLang(READER_STATE.original.lang);
    const r = await fetch(
      `/api/translate?url=${encodeURIComponent(READER_STATE.url)}&lang=${tgt}`,
    );
    const data = await r.json();
    if (data.error || !data.paragraphs) {
      if (btn) {
        btn?.classList.remove("loading");
        if (btn) btn.disabled = false;
        if (btn) btn.title = data.error || "Translation failed";
      }
      return;
    }
    READER_STATE.translated = data;
    READER_STATE.view = "translated";
    // renderReader rebuilds the button with the "원문 / Original" label
    // and clears any loading state.
    renderReader(content, data, READER_STATE.item);
    // If we just translated an English article INTO Korean, stamp the
    // feed-card data so the ✦한 badge appears on the wall without
    // waiting for the next /api/brief tick. KO→EN doesn't touch the
    // card affordance (card already in Korean for Korean readers).
    const tgtLang = _targetLang(READER_STATE.original.lang);
    if (tgtLang === "ko" && data.title && data.paragraphs && data.paragraphs.length) {
      const url = READER_STATE.url;
      const stamp = (list) => {
        if (!Array.isArray(list)) return;
        for (const m of list) {
          if (m && m.url === url) {
            m.title_ko = data.title;
            m.dek_ko   = (data.paragraphs[0] || "").slice(0, 280);
            m.translated_at = Math.floor(Date.now() / 1000);
          }
        }
      };
      stamp(STATE.mixed);
      stamp(PAGE_OVERRIDE);
      rerenderCard(url);
    }
  } catch (e) {
    console.warn("translate failed:", e);
    if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
  }
}

// Build the translate pill inline. Rebuilt on every renderReader call,
// so a stale state can't outlive a render. Click handler attached to
// the fresh element directly.
function buildTranslateButton() {
  const data = READER_STATE.original;
  if (!data) return null;
  const srcLang = (data.lang || "en").toLowerCase();
  const isTranslated = READER_STATE.view === "translated";
  let labelText, labelLang;
  if (isTranslated) {
    labelText = srcLang === "ko" ? "원문" : "Original";
    labelLang = srcLang === "ko" ? "ko" : "en";
  } else if (srcLang === "ko") {
    labelText = "English";
    labelLang = "en";
  } else {
    labelText = "한국어";
    labelLang = "ko";
  }
  const btn = null; if (false) el("button", {
    class: "reader-translate" + (isTranslated ? " active" : ""),
    type: "button",
    title: isTranslated ? "Show original" : "Translate",
    "aria-label": "translate toggle",
  }, [
    el("span", { class: "rt-glyph", "aria-hidden": "true" }, "✦"),
    el("span", { class: "rt-label", lang: labelLang }, labelText),
  ]);
  btn?.addEventListener("click", toggleTranslation);
  return btn;
}


function renderReader(content, data, item) {
  const lang = data.lang || item.lang || "en";
  // The feed URL, not final_url — this is the key everything else in
  // the app (delete, siblings, saved list) is stored under.
  const articleUrlForNav = data.url || item.url || READER_STATE.url;
  content.innerHTML = "";   // the static .reader-close button lives OUTSIDE
                            // .reader-content, so this clears only the body.

  // Hero photo. A real <img> rather than a background-image div so the
  // box takes the photo's own aspect ratio: full-bleed width, height
  // follows the source, nothing cropped off the top or bottom. A fixed
  // 16/9 background box chopped the subject out of portrait and square
  // press photos. If the publisher's CDN 404s we drop the element
  // entirely instead of leaving an empty grey slab.
  const imgSrc = data.image || item.image;
  if (imgSrc) {
    const heroWrap = el("div", { class: "reader-img-wrap" });
    const hero = el("img", {
      class: "reader-img",
      src: imgSrc,
      alt: "",
      loading: "eager",
      decoding: "async",
    });
    hero.addEventListener("error", () => heroWrap.remove());
    hero.addEventListener("load", () => heroWrap.classList.add("loaded"));
    heroWrap.appendChild(hero);
    content.appendChild(heroWrap);
  }


  // Build the meta strip. Word-count + formal date inherit the same
  // mono-uppercase 10.5px / 0.8px letter-spacing from .reader-meta.
  const dateLabel = fmtFormal(item.ts);
  const srcLang = (data.lang || lang || "en").toLowerCase();
  const langPip = el("span", {
    class: `reader-lang lang-${srcLang}`,
    title: "Article language",
  }, srcLang.toUpperCase());
  const metaEl = el("div", { class: "reader-meta" }, [
    item.outlet ? el("span", { class: "src" }, item.outlet) : null,
    item.category ? el("span", { class: "tag" }, item.category.toUpperCase()) : null,
    langPip,
    data.byline ? el("span", {}, data.byline) : null,
    // Reading time reads better than a raw word count, and it's the
    // number people actually decide on. Falls back to the word count
    // when the body is only a stored summary.
    (() => {
      const mins = readingMinutes(data.paragraphs, lang);
      if (!mins) return null;
      return el("span", { class: "reader-stats" },
        lang === "ko" ? `${mins}분 읽기` : `${mins} MIN READ`);
    })(),
    dateLabel ? el("span", { class: "reader-stats" }, dateLabel) : null,
  ]);
  // Translate pill rendered inline at the end of the meta strip.
  // Built fresh every renderReader call so a stale element can't
  // outlive a state change. Click handler is attached at build time.
  const translateBtn = buildTranslateButton();
  if (translateBtn) metaEl.appendChild(translateBtn);
  content.appendChild(metaEl);

  content.appendChild(el("h1", { class: "reader-title", lang },
    data.title || item.title || "(no title)"));

  // Translator note — ONLY shown when the model explicitly added one
  // (e.g. "이미지 캡션 생략" / "summarised from 1200 words"). The old
  // behaviour of showing a generic "AI 번역 · 요약본" pip whenever
  // data.summarized was true was misleading on short articles where
  // nothing was actually compressed.
  if (data.translated && data.note && data.note.trim()) {
    content.appendChild(el("div", { class: "reader-tnote", lang }, data.note.trim()));
  }

  if (data.paragraphs && data.paragraphs.length) {
    const body = el("div", { class: "reader-body" });
    data.paragraphs.forEach((p) => body.appendChild(el("p", { lang }, p)));
    content.appendChild(body);
  }

  // In-reader delete button — uses the same reason-picker modal +
  // /api/article/delete endpoint as the × on the card. The article
  // gets permanently dropped from `articles` + `reader_results` and
  // its URL is added to `blocked_urls` so RSS re-ingest can't bring
  // it back. Close the reader first, then open the picker so the two
  // modals don't fight over body-scroll lock.
  const articleUrl = articleUrlForNav;
  const articleTitle = data.title || item.title || "";
  const delBtn = el("button", {
    class: "reader-delete",
    type: "button",
    title: "permanently remove this article from the feed",
  }, "× DELETE ARTICLE");
  delBtn.addEventListener("click", (e) => {
    e.preventDefault();
    if (!articleUrl) return;
    closeReader();
    setTimeout(() => openDeleteModal(articleUrl, articleTitle), 100);
  });

  // ── Related stories ────────────────────────────────────────
  // Same category first, same outlet as backup. Rendered only when we
  // actually found something; an empty "Related" heading is noise.
  const related = relatedItems(item || {});
  if (related.length) {
    content.appendChild(el("section", { class: "reader-related" }, [
      el("h2", { class: "reader-related-h" },
        lang === "ko" ? "관련 기사" : "Related"),
      el("ul", { class: "reader-related-list" }, related.map((m) =>
        el("li", {}, [
          el("a", {
            href: m.url,
            class: "reader-related-item",
            "data-reader-jump": m.url,
          }, [
            el("span", { class: "rr-outlet" }, m.outlet || ""),
            el("span", { class: "rr-title", lang: m.lang || "en" }, m.title || m.url),
          ]),
        ]))),
    ]));
  }

  // ── Prev / next ────────────────────────────────────────────
  // Follows the order of the page the user is looking at, so it behaves
  // like paging through the feed rather than jumping somewhere random.
  const sibs = readerSiblings(articleUrlForNav);
  if (sibs.prev || sibs.next) {
    content.appendChild(el("nav", { class: "reader-nav" }, [
      sibs.prev
        ? el("a", {
            href: sibs.prev.url, class: "reader-nav-btn prev",
            "data-reader-jump": sibs.prev.url,
          }, [
            el("span", { class: "rn-dir" }, lang === "ko" ? "‹ 이전" : "‹ PREV"),
            el("span", { class: "rn-title" }, sibs.prev.title || ""),
          ])
        : el("span", { class: "reader-nav-btn empty" }),
      sibs.next
        ? el("a", {
            href: sibs.next.url, class: "reader-nav-btn next",
            "data-reader-jump": sibs.next.url,
          }, [
            el("span", { class: "rn-dir" }, lang === "ko" ? "다음 ›" : "NEXT ›"),
            el("span", { class: "rn-title" }, sibs.next.title || ""),
          ])
        : el("span", { class: "reader-nav-btn empty" }),
    ]));
  }

  content.appendChild(el("div", { class: "reader-footer" }, [
    el("span", { class: "badge" }, "✦ dailybrief reader"),
    el("a", {
      // final_url is the publisher URL after unwrapping Google News —
      // without it this button dumped the user back on a Google
      // interstitial for every Google-sourced story.
      href: data.final_url || data.url || item.url,
      target: "_blank",
      rel: "noopener",
      class: "reader-original",
    }, "open original ↗"),
    delBtn,
  ]));
}

function closeReader() {
  document.getElementById("reader").classList.add("hidden");
  unlockBodyScroll();
  READER_STATE = {
    url: null, item: null, original: null, translated: null,
    view: "original",
  };
  // If a refresh was deferred while we were reading, run it now —
  // background articles update without jolting the user's scroll
  // position during their read.
  if (PENDING_REFRESH) {
    PENDING_REFRESH = false;
    setTimeout(silentRefresh, 250);
  }
}

// ── Flow detail modal (whales / trades / videos) ───────────────
// CRITICAL: capture scrollY BEFORE applying position:fixed. iOS Safari
// (and some Chrome versions) snap the visual scroll to 0 the moment a
// position:fixed body is committed, so reading window.scrollY AFTER
// the style change yielded 0 — that's why closing the reader always
// scrolled the page back to the top.
function lockBodyScroll() {
  if (document.body.dataset.scrollLocked === "1") return;
  const y = window.scrollY || window.pageYOffset || 0;
  document.body.dataset.scrollY = String(y);
  document.body.dataset.scrollLocked = "1";
  document.body.style.overflow = "hidden";
  document.body.style.position = "fixed";
  document.body.style.left = "0";
  document.body.style.right = "0";
  document.body.style.top = `-${y}px`;
  document.body.style.width = "100%";
}
function unlockBodyScroll() {
  if (document.body.dataset.scrollLocked !== "1") return;
  const y = parseInt(document.body.dataset.scrollY || "0", 10);
  // Order matters on iOS Safari: tear down position:fixed FIRST, then
  // scroll in the next animation frame so the layout has been
  // recomputed and scrollTo isn't fighting the fixed-body teardown.
  document.body.style.overflow = "";
  document.body.style.position = "";
  document.body.style.top = "";
  document.body.style.left = "";
  document.body.style.right = "";
  document.body.style.width = "";
  delete document.body.dataset.scrollLocked;
  // Triple-pronged restore — covers Safari, Chrome, older browsers.
  // `behavior: instant` skips smooth-scroll animation; we want to
  // teleport back to the exact pre-lock pixel.
  const restore = () => {
    try { window.scrollTo({ top: y, left: 0, behavior: "instant" }); }
    catch { window.scrollTo(0, y); }
    document.documentElement.scrollTop = y;
    document.body.scrollTop = y;          // legacy Safari
  };
  restore();
  // Some browsers commit the position:fixed teardown async; restore
  // again on the next frame to catch them.
  requestAnimationFrame(restore);
}

// ── Swipe-to-close (iOS-style sheet) ──────────────────────────
// Arms only at scroll edges (top → pull-down closes, bottom →
// pull-up closes); mid-article touches scroll normally. Smooth
// quart-out fly-off on commit, spring back on cancel.
function attachSwipeToClose(modalSelector, closeFn) {
  const modal = document.querySelector(modalSelector);
  if (!modal) return;
  const card = modal.querySelector(".reader-card");
  if (!card) return;

  // Easing curves
  const EASE_FLY  = "cubic-bezier(0.22, 1, 0.36, 1)";   // ease-out-quart
  const EASE_SNAP = "cubic-bezier(0.34, 1.56, 0.64, 1)"; // spring-back overshoot
  const FLY_MS    = 380;
  const SNAP_MS   = 320;

  // Rubber-band damping — drag past the threshold and resistance grows.
  function damp(delta) {
    const t = Math.min(1, Math.abs(delta) / 400);
    const k = 1 - (1 - t) * (1 - t);   // ease-out-quad
    const factor = 1 - k * 0.35;       // shrink the trailing portion
    return delta * factor;
  }

  let startY = null;
  let lastY = null;
  let lastTs = 0;
  let velocity = 0;       // px/ms, EMA over recent moves
  let atTop = false;
  let atBottom = false;
  let armed = false;
  let commitReady = false;
  // Higher arm threshold so accidental finger micro-movements while
  // scrolling at the edge don't immediately hijack into a close gesture.
  // Higher dismiss distance so the user must clearly commit. A fast
  // flick still closes via VELOCITY_DISMISS even at shorter distance.
  const ARM_PX = 16;
  const DISMISS_DELTA = 140;
  const VELOCITY_DISMISS = 0.9;   // px/ms — quick flick threshold
  const VELOCITY_MIN_DELTA = 60;  // even a flick needs some distance

  function setCommitReady(next) {
    if (next === commitReady) return;
    commitReady = next;
    card.classList.toggle("drag-commit", next);
    if (next && typeof navigator !== "undefined" && navigator.vibrate) {
      try { navigator.vibrate(8); } catch (_) {}
    }
  }

  function clearTransitions() {
    card.style.transition = "";
    card.style.willChange = "";
    const bd = modal.querySelector(".reader-backdrop");
    if (bd) bd.style.transition = "";
  }

  card.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    startY = e.touches[0].clientY;
    lastY = startY;
    lastTs = performance.now();
    velocity = 0;
    atTop = card.scrollTop <= 0;
    atBottom = card.scrollTop + card.clientHeight >= card.scrollHeight - 1;
    armed = false;
    setCommitReady(false);
  }, { passive: true });

  card.addEventListener("touchmove", (e) => {
    if (startY === null) return;
    const y = e.touches[0].clientY;
    const now = performance.now();
    const dt = Math.max(1, now - lastTs);
    // EMA over recent velocity, sign preserved (positive = downward)
    velocity = 0.7 * velocity + 0.3 * ((y - lastY) / dt);
    lastY = y;
    lastTs = now;
    const rawDelta = lastY - startY;

    if (!armed) {
      if (rawDelta > ARM_PX && atTop) {
        armed = true;
        card.classList.add("dragging");
      } else if (rawDelta < -ARM_PX && atBottom) {
        armed = true;
        card.classList.add("dragging");
      } else {
        return;
      }
    }

    const d = damp(rawDelta);
    card.style.transition = "none";
    card.style.willChange = "transform, opacity";
    // Visual progress only reaches "near-commit" at DISMISS_DELTA, not before.
    const progress = Math.min(1, Math.abs(rawDelta) / (DISMISS_DELTA * 1.4));
    const scale = 1 - progress * 0.05;
    const opacity = 1 - progress * 0.25;
    card.style.transform = `translateY(${d}px) scale(${scale})`;
    card.style.opacity = String(opacity);
    const backdrop = modal.querySelector(".reader-backdrop");
    if (backdrop) backdrop.style.opacity = String(Math.max(0.1, 1 - progress * 0.85));

    setCommitReady(Math.abs(rawDelta) >= DISMISS_DELTA);
  }, { passive: true });

  card.addEventListener("touchend", () => {
    if (startY === null) return;
    card.classList.remove("dragging");
    if (!armed) {
      setCommitReady(false);
      startY = null; lastY = null;
      return;
    }

    const rawDelta = (lastY ?? startY) - startY;
    const backdrop = modal.querySelector(".reader-backdrop");
    // Same direction as drag → flick counts. Slow drag must rely on distance.
    const sameDir = (rawDelta > 0 && velocity > 0) || (rawDelta < 0 && velocity < 0);
    const flick = sameDir && Math.abs(velocity) > VELOCITY_DISMISS && Math.abs(rawDelta) > VELOCITY_MIN_DELTA;
    const shouldDismiss = flick || Math.abs(rawDelta) > DISMISS_DELTA;

    if (shouldDismiss) {
      // Commit close: smooth fly-off, fade, scale-down, then closeFn.
      card.style.transition = `transform ${FLY_MS}ms ${EASE_FLY}, opacity ${FLY_MS}ms ${EASE_FLY}`;
      if (backdrop) backdrop.style.transition = `opacity ${FLY_MS}ms ${EASE_FLY}`;
      card.style.transform = `translateY(${rawDelta > 0 ? "100vh" : "-100vh"}) scale(0.92)`;
      card.style.opacity = "0";
      if (backdrop) backdrop.style.opacity = "0";
      setTimeout(() => {
        closeFn();
        card.style.transform = "";
        card.style.opacity = "";
        if (backdrop) backdrop.style.opacity = "";
        clearTransitions();
      }, FLY_MS);
    } else {
      // Snap back with a soft spring.
      card.style.transition = `transform ${SNAP_MS}ms ${EASE_SNAP}, opacity ${SNAP_MS}ms ${EASE_SNAP}`;
      if (backdrop) backdrop.style.transition = `opacity ${SNAP_MS}ms ${EASE_SNAP}`;
      card.style.transform = "";
      card.style.opacity = "";
      if (backdrop) backdrop.style.opacity = "";
      setTimeout(clearTransitions, SNAP_MS + 20);
    }

    startY = null;
    lastY = null;
    armed = false;
    setCommitReady(false);
  });

  card.addEventListener("touchcancel", () => {
    card.classList.remove("dragging");
    setCommitReady(false);
    startY = null;
    lastY = null;
    armed = false;
    clearTransitions();
    card.style.transform = "";
    card.style.opacity = "";
  });
}

// ── Change-password modal ──────────────────────────────────────
function openPwModal() {
  const modal = document.getElementById("pw-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  lockBodyScroll();
  // Reset form + status line
  const form = document.getElementById("pw-form");
  if (form) form.reset();
  const msg = document.getElementById("pw-msg");
  if (msg) { msg.textContent = ""; msg.classList.remove("ok", "err"); }
  // Focus the first field after the modal animation settles
  setTimeout(() => document.getElementById("pw-old")?.focus(), 60);
}
function closePwModal() {
  const modal = document.getElementById("pw-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  unlockBodyScroll();
}
async function submitPwChange(e) {
  e.preventDefault();
  const submit = document.getElementById("pw-submit");
  const msg = document.getElementById("pw-msg");
  const oldPw = document.getElementById("pw-old").value;
  const newPw = document.getElementById("pw-new").value;
  const confirm = document.getElementById("pw-confirm").value;
  msg.classList.remove("ok", "err");
  if (newPw !== confirm) {
    msg.textContent = "New passwords don't match.";
    msg.classList.add("err");
    return;
  }
  if (newPw.length < 4) {
    msg.textContent = "New password must be at least 4 characters.";
    msg.classList.add("err");
    return;
  }
  submit.disabled = true;
  submit.textContent = "Updating…";
  try {
    const r = await fetch("/api/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      msg.textContent = d.detail || `Update failed (HTTP ${r.status})`;
      msg.classList.add("err");
      return;
    }
    msg.textContent = "✓ Password updated.";
    msg.classList.add("ok");
    setTimeout(closePwModal, 900);
  } catch (err) {
    msg.textContent = err.message || "Network error";
    msg.classList.add("err");
  } finally {
    submit.disabled = false;
    submit.textContent = "Update";
  }
}

// ── Delete-with-reason modal ───────────────────────────────────
// Tracks which URL the open modal is acting on; reset on close.
let DELETE_TARGET = { url: null, title: null };

function openDeleteModal(url, title) {
  const modal = document.getElementById("delete-modal");
  if (!modal) return;
  DELETE_TARGET = { url, title: title || "" };
  const headline = document.getElementById("delete-headline");
  if (headline) headline.textContent = title || url;
  // Clear any leftover error / submitting state from a previous open.
  const errEl = document.getElementById("delete-error");
  if (errEl) { errEl.textContent = ""; errEl.classList.remove("visible"); }
  const reasons = document.getElementById("delete-reasons");
  if (reasons) reasons.classList.remove("submitting");
  reasons?.querySelectorAll(".submitting-active")
    .forEach((b) => b.classList.remove("submitting-active"));
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  lockBodyScroll();
}

function closeDeleteModal() {
  const modal = document.getElementById("delete-modal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  DELETE_TARGET = { url: null, title: null };
  unlockBodyScroll();
}

// Removes the card from every in-memory list + drops the DOM node
// with a quick fade. Used after a successful /api/article/delete.
function purgeUrlLocally(url) {
  if (!url) return;
  if (STATE.mixed)     STATE.mixed = STATE.mixed.filter((m) => m.url !== url);
  if (PAGE_OVERRIDE)   PAGE_OVERRIDE = PAGE_OVERRIDE.filter((m) => m.url !== url);
  document.querySelectorAll(`.art[data-url="${CSS.escape(url)}"]`).forEach((n) => {
    n.classList.add("art-vanish");
    setTimeout(() => n.remove(), 280);
  });
}

async function confirmDelete(reason) {
  const url = DELETE_TARGET.url;
  if (!url) { closeDeleteModal(); return; }

  // Lock the picker buttons while we wait so the user can't fire the
  // request twice + so it's clear something is happening.
  const reasonsBlock = document.getElementById("delete-reasons");
  if (reasonsBlock) reasonsBlock.classList.add("submitting");
  const clickedBtn = reasonsBlock?.querySelector(`[data-reason="${reason}"]`);
  if (clickedBtn) clickedBtn.classList.add("submitting-active");

  // Fire FIRST, await the response, then act on success. The old
  // optimistic-purge pattern hid auth failures from the user — the
  // card vanished visually but the DB row stayed, so refresh brought
  // it back. Now the card only goes away when the server confirms.
  let serverOk = false;
  let errMsg = null;
  try {
    const r = await fetch("/api/article/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, reason }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok !== false) {
      serverOk = true;
    } else if (r.status === 401) {
      errMsg = "You need to sign in to delete articles.";
    } else if (r.status === 403) {
      errMsg = "Only admins can delete articles.";
    } else {
      errMsg = d.detail || `Delete failed (HTTP ${r.status})`;
    }
  } catch (e) {
    errMsg = e.message || "Network error";
  }

  if (reasonsBlock) reasonsBlock.classList.remove("submitting");
  if (clickedBtn) clickedBtn.classList.remove("submitting-active");

  if (serverOk) {
    closeDeleteModal();
    purgeUrlLocally(url);
  } else {
    // Surface the error inside the modal so the user understands the
    // article was NOT deleted. Anonymous users now get a clear "sign
    // in required" instead of a silent failure that confused
    // everyone before.
    let msgEl = document.getElementById("delete-error");
    if (!msgEl) {
      msgEl = el("div", { id: "delete-error", class: "delete-error" }, "");
      const card = document.querySelector("#delete-modal .delete-card");
      const actions = card?.querySelector(".delete-actions");
      if (card && actions) card.insertBefore(msgEl, actions);
    }
    msgEl.textContent = "⚠ " + (errMsg || "Delete failed");
    msgEl.classList.add("visible");
  }
}

// ── Settings popover ───────────────────────────────────────────
// Tiny menu hung off the ⚙ gear next to the date. Two practical
// knobs only: text-size (S/M/L/XL) and a manual refresh trigger.
function applyTextScale(scale) {
  document.documentElement.style.setProperty("--text-scale", String(scale));
  try { localStorage.setItem("dailybrief.textScale", String(scale)); } catch {}
  document.querySelectorAll("#text-scale-segs .seg").forEach((b) => {
    b.classList.toggle("active", parseFloat(b.dataset.scale) === scale);
  });
}
function toggleSettings(force) {
  const pop = document.getElementById("settings-pop");
  if (!pop) return;
  const show = typeof force === "boolean" ? force : pop.classList.contains("hidden");
  pop.classList.toggle("hidden", !show);
  pop.setAttribute("aria-hidden", show ? "false" : "true");
  // Lock body scroll while the popover is up. Class also reveals the
  // backdrop (.settings-backdrop) via .settings-open in CSS.
  document.body.classList.toggle("settings-open", show);
  // Lazy-load the keys section the first time we open after admin login.
  if (show) refreshKeysSection();
}

// ── API key management (admin only) ─────────────────────────────
// Provider detection mirrors the server-side _detect_provider so the
// badge changes the instant the user pastes — no round-trip needed.
// Validation, persistence, and the available-models list still come
// from the server.
const KEY_PROVIDERS = {
  anthropic: { label: "Claude",  prefix: "sk-ant-", color: "#ff9a3c" },
  gemini:    { label: "Gemini",  prefix: "AIza",    color: "#00e1ff" },
};

function detectProviderLocal(raw) {
  const k = (raw || "").trim();
  for (const [name, p] of Object.entries(KEY_PROVIDERS)) {
    if (k.startsWith(p.prefix) && k.length > p.prefix.length + 8) return name;
  }
  return null;
}

async function refreshKeysSection() {
  const section = document.getElementById("settings-keys-section");
  if (!section) return;
  // Probe admin status. /api/admin/keys returns 401/403 for non-admins.
  let payload = null;
  try {
    const r = await fetch("/api/admin/keys", { credentials: "same-origin" });
    if (!r.ok) {
      section.hidden = true;
      return;
    }
    payload = await r.json();
  } catch {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  // Render active keys (the ones already configured).
  const active = document.getElementById("keys-active");
  active.innerHTML = "";
  for (const [provider, info] of Object.entries(payload)) {
    if (!info.set) continue;
    const chip = document.createElement("div");
    chip.className = "key-active-chip";
    chip.innerHTML = `
      <span><span class="kac-name">${info.label}</span></span>
      <span class="kac-mask">${info.masked || ""}</span>
      <button class="kac-remove" type="button" data-provider="${provider}" aria-label="Remove ${info.label} key">Remove</button>
    `;
    active.appendChild(chip);
  }
  // Wire remove buttons.
  active.querySelectorAll(".kac-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const provider = btn.dataset.provider;
      btn.disabled = true;
      try {
        const r = await fetch(`/api/admin/keys/${provider}`, {
          method: "DELETE",
          credentials: "same-origin",
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "remove failed");
        await refreshKeysSection();
      } catch (e) {
        const msg = document.getElementById("key-msg");
        if (msg) { msg.className = "key-msg err"; msg.textContent = e.message || "remove failed"; }
        btn.disabled = false;
      }
    });
  });
}

function wireApiKeyUI() {
  const input = document.getElementById("key-input");
  const badge = document.getElementById("key-provider-badge");
  const btn   = document.getElementById("key-save-btn");
  const msg   = document.getElementById("key-msg");
  const modelsBox = document.getElementById("key-models");
  const modelSelect = document.getElementById("key-model-select");
  if (!input || !badge || !btn || !msg) return;

  function setBadge(provider) {
    if (!provider) {
      badge.hidden = true;
      badge.classList.remove("bad");
      btn.disabled = !input.value.trim();
      // If user typed something but we don't recognize it, mark bad.
      if (input.value.trim().length > 6) {
        badge.hidden = false;
        badge.classList.add("bad");
        badge.textContent = "?";
      }
    } else {
      badge.hidden = false;
      badge.classList.remove("bad");
      badge.textContent = KEY_PROVIDERS[provider].label;
      btn.disabled = false;
    }
  }

  input.addEventListener("input", () => {
    msg.className = "key-msg";
    msg.textContent = "";
    setBadge(detectProviderLocal(input.value));
  });

  btn.addEventListener("click", async () => {
    const key = input.value.trim();
    if (!key) return;
    btn.classList.add("loading");
    btn.disabled = true;
    msg.className = "key-msg";
    msg.textContent = "";
    try {
      const r = await fetch("/api/admin/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ key }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || "save failed");
      msg.className = "key-msg ok";
      msg.textContent = `✓ ${data.label} key saved (${data.masked})`;
      input.value = "";
      setBadge(null);
      // Populate the model picker.
      if (data.models && data.models.length && modelSelect) {
        modelSelect.innerHTML = data.models
          .map((m) => `<option value="${m}">${m}</option>`)
          .join("");
        modelsBox.hidden = false;
        // Restore preferred model for this provider, if any.
        try {
          const saved = localStorage.getItem(`dailybrief.model.${data.provider}`);
          if (saved && data.models.includes(saved)) modelSelect.value = saved;
        } catch {}
      }
      await refreshKeysSection();
    } catch (e) {
      msg.className = "key-msg err";
      msg.textContent = e.message || "save failed";
    } finally {
      btn.classList.remove("loading");
      btn.disabled = !input.value.trim();
    }
  });

  if (modelSelect) {
    modelSelect.addEventListener("change", () => {
      const provider = badge.textContent === KEY_PROVIDERS.anthropic.label
        ? "anthropic"
        : badge.textContent === KEY_PROVIDERS.gemini.label ? "gemini" : null;
      // Best-effort: persist per-provider model preference client-side.
      // Server doesn't read it yet — wire up consumer-side in a follow-up
      // pass so the curator/translator respect this choice.
      try {
        if (provider) localStorage.setItem(`dailybrief.model.${provider}`, modelSelect.value);
      } catch {}
    });
  }
}

// ── Wire up ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  }).toUpperCase();

  // Swipe gestures on the reader + flow modals (mobile dismiss UX)
  attachSwipeToClose("#reader", closeReader);
  // (flow modal removed)

  // Restore saved text scale
  let savedScale = 1;
  try {
    const s = parseFloat(localStorage.getItem("dailybrief.textScale") || "1");
    if (s >= 0.7 && s <= 2) savedScale = s;
  } catch {}
  applyTextScale(savedScale);

  // Settings ⚙
  $("#settings-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleSettings();
  });
  // Wire up the API key input + save button. The section stays hidden
  // until refreshKeysSection() succeeds (admin-only).
  wireApiKeyUI();
  $("#text-scale-segs")?.addEventListener("click", (e) => {
    const seg = e.target.closest(".seg");
    if (!seg) return;
    applyTextScale(parseFloat(seg.dataset.scale));
  });

  // Refresh-rate segmented control — set localStorage; the smartCheck
  // tick reads on each probe so the change applies immediately.
  function applyRefreshSeg(seconds) {
    try { localStorage.setItem("dailybrief.refreshSeconds", String(seconds)); } catch {}
    document.querySelectorAll("#refresh-segs .seg").forEach((b) => {
      b.classList.toggle("active", parseInt(b.dataset.refresh, 10) === seconds);
    });
  }
  let savedRefresh = 1800;
  try {
    const v = parseInt(localStorage.getItem("dailybrief.refreshSeconds") || "1800", 10);
    if (!isNaN(v) && v >= 0) savedRefresh = v;
  } catch {}
  applyRefreshSeg(savedRefresh);
  $("#refresh-segs")?.addEventListener("click", (e) => {
    const seg = e.target.closest(".seg");
    if (!seg) return;
    applyRefreshSeg(parseInt(seg.dataset.refresh, 10));
  });

  // Interest categories — toggleable chips. Clicking persists +
  // re-paints the feed so the change is visible right away.
  function paintInterests() {
    document.querySelectorAll("#interest-grid .interest").forEach((b) => {
      b.classList.toggle("active", INTERESTS.has(b.dataset.cat));
    });
  }
  paintInterests();
  $("#interest-grid")?.addEventListener("click", (e) => {
    const b = e.target.closest(".interest");
    if (!b) return;
    const cat = b.dataset.cat;
    if (INTERESTS.has(cat)) INTERESTS.delete(cat);
    else INTERESTS.add(cat);
    _persistInterests();
    paintInterests();
    paint(false);
  });

  // Account state + admin gate. /whoami returns {user: {username,
  // is_admin, subscription}} when logged in, {user: null} otherwise.
  // We use this to: (1) show login/logout/change-pw in the settings
  // popover, (2) paint the masthead account chip with the right
  // state, (3) mirror is_admin onto body[data-is-admin] so CSS can
  // hide delete buttons. The backend /api/article/delete independently
  // requires admin, so DevTools tampering can't actually delete.
  function paintAccount(user) {
    const loggedIn = !!user;
    const isAdmin = !!(user && user.is_admin);

    // Settings popover account row — five states, each link gated.
    const loginA   = document.getElementById("settings-login");
    const logoutA  = document.getElementById("settings-logout");
    const changeA  = document.getElementById("settings-changepw");
    const accountA = document.getElementById("settings-account");
    const labA     = document.getElementById("settings-lab");
    if (loginA)   loginA.hidden   = loggedIn;
    if (logoutA)  logoutA.hidden  = !loggedIn;
    if (changeA)  changeA.hidden  = !loggedIn;
    if (accountA) accountA.hidden = !loggedIn;
    // Lab is strictly admin-only — anonymous + regular users don't even
    // see the link. Backend /lab also redirects them to /login, so the
    // gate is enforced on both sides.
    if (labA)     labA.hidden     = !isAdmin;

    // Body attribute drives the CSS delete-button gate.
    if (isAdmin) document.body.dataset.isAdmin = "1";
    else         delete document.body.dataset.isAdmin;

    // Masthead account glyph — same 22×22 square as the ⚙. Single
    // character inside, color/border tells the state.
    //   anonymous → ◌ (dotted ring, suggests "no account yet")
    //   user      → ◉ (filled bullseye — active account)
    //   admin     → ◈ (lozenge — distinctly admin)
    const chip = document.getElementById("account-btn");
    if (!chip) return;
    chip.classList.remove("account-anon", "account-user", "account-admin");
    if (!loggedIn) {
      chip.classList.add("account-anon");
      chip.title = "Click to sign in";
      chip.textContent = "◌";
    } else if (isAdmin) {
      chip.classList.add("account-admin");
      chip.title = `${user.username} · admin`;
      chip.textContent = "◈";
    } else {
      chip.classList.add("account-user");
      chip.title = user.username;
      chip.textContent = "◉";
    }

    // Change-pw modal sub-line shows who's logged in.
    const sub = document.getElementById("pw-current-user");
    if (sub) sub.textContent = loggedIn ? `Signed in as ${user.username}` : "Not signed in";
  }
  fetch("/api/auth/whoami").then((r) => r.json()).then((d) => {
    paintAccount((d && d.user) || null);
  }).catch(() => paintAccount(null));

  // Masthead account button — anonymous → /login, logged-in → /account.
  // Direct jump to the profile page is more intuitive than the settings
  // popover dance, and the popover is one ⚙ click away anyway.
  document.getElementById("account-btn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const anon = document.getElementById("account-btn").classList.contains("account-anon");
    location.href = anon ? "/login" : "/account";
  });

  // Change-password link in settings → open modal.
  document.getElementById("settings-changepw")?.addEventListener("click", (e) => {
    e.preventDefault();
    toggleSettings(false);
    openPwModal();
  });
  document.querySelectorAll('#pw-modal [data-close="pw"]').forEach((n) =>
    n.addEventListener("click", closePwModal));
  document.getElementById("pw-form")?.addEventListener("submit", submitPwChange);
  document.getElementById("settings-logout")?.addEventListener("click", async (e) => {
    e.preventDefault();
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch {}
    location.reload();
  });
  // Click outside / Escape closes the popover
  document.addEventListener("click", (e) => {
    const pop = document.getElementById("settings-pop");
    if (!pop || pop.classList.contains("hidden")) return;
    if (e.target.closest("#settings-pop") || e.target.closest("#settings-btn")) return;
    toggleSettings(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggleSettings(false);
  });

  wireSearch();

  $("#chips").addEventListener("click", async (e) => {
    // The ▾ expand toggle lives inside the nav but isn't a chip.
    if (e.target.closest("#chip-toggle")) return;
    const chip = e.target.closest(".chip");
    if (!chip) return;
    // Picking a category is an explicit "show me the feed" — leave
    // search mode rather than filtering results the user can't see.
    if (SEARCH_Q) exitSearch();
    CAT = chip.dataset.cat;
    PAGE = 1;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    // Category views (and premium) pull from the DB so the user sees
    // the full archive for that category, not just the lean wire slice.
    PAGE_OVERRIDE = null; PAGE_OVERRIDE_N = null;
    if (CAT !== "all") await fetchPage(1);
    paint();
  });

  // ── Chip nav overflow / expand toggle (phone) ────────────────
  // On mobile, .chips is clamped to ~2 rows. If the natural height
  // exceeds the clamp, show the ▾ button; clicking it flips the
  // .expanded state and the glyph rotates 180°.
  const chipsEl = document.getElementById("chips");
  const chipToggleEl = document.getElementById("chip-toggle");
  function syncChipToggle() {
    if (!chipsEl || !chipToggleEl) return;
    // Desktop / tablet ≥ 769px: clamp + toggle both inactive.
    if (window.matchMedia("(min-width: 769px)").matches) {
      chipsEl.classList.remove("expanded");
      chipToggleEl.hidden = true;
      return;
    }
    // Measure natural height by temporarily lifting the clamp.
    const wasExpanded = chipsEl.classList.contains("expanded");
    chipsEl.classList.add("expanded");
    const natural = chipsEl.scrollHeight;
    if (!wasExpanded) chipsEl.classList.remove("expanded");
    // Threshold: anything noticeably taller than ~2 rows worth.
    chipToggleEl.hidden = natural <= 52;
  }
  chipToggleEl?.addEventListener("click", () => {
    chipsEl?.classList.toggle("expanded");
  });
  syncChipToggle();
  window.addEventListener("resize", syncChipToggle);
  // Re-check after fonts load (chip widths shift a touch).
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(syncChipToggle).catch(() => {});
  }
  // Intercept article-card clicks → open the reader modal.
  // ⌘/Ctrl/Shift/middle-click keeps the default behaviour (new tab).
  document.addEventListener("click", (e) => {
    // × delete button on a card has priority. Must run BEFORE any
    // other handler so the click can never bubble up to the parent
    // anchor.
    const delBtn = e.target.closest("[data-delete]");
    if (delBtn) {
      e.preventDefault();
      e.stopPropagation();
      const art = delBtn.closest(".art");
      const url = art?.getAttribute("data-url") || art?.getAttribute("href");
      const titleNode = art?.querySelector(".h");
      const title = titleNode ? titleNode.textContent : "";
      if (url) openDeleteModal(url, title);
      return;
    }
    // ✦한 translation toggle — flip the card's preview between
    // English source and Korean translation. Never opens the reader.
    const koBtn = e.target.closest("[data-ko-toggle]");
    if (koBtn) {
      e.preventDefault();
      e.stopPropagation();
      const art = koBtn.closest(".art");
      const url = art?.getAttribute("data-url") || art?.getAttribute("href");
      if (!url) return;
      if (CARD_KO_URLS.has(url)) CARD_KO_URLS.delete(url);
      else CARD_KO_URLS.add(url);
      _persistCardKo();
      if (!rerenderCard(url)) paint(false);
      return;
    }
    const art = e.target.closest("#paper .art, #paper-noimg .art");
    if (!art) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    const url = art.getAttribute("href");
    const item = STATE.mixed.find((m) => m.url === url) || {};
    openReader(url, item);
  });

  // Close-on-button + backdrop + Escape for both modals. The static
  // ×/backdrop in index.html are each wired ONCE here.
  // Related / prev / next links swap the article in place instead of
  // navigating away — the reader stays open and scroll resets to the
  // top, which is what "next article" should feel like.
  document.querySelector("#reader .reader-content")
    ?.addEventListener("click", (e) => {
      const jump = e.target.closest("[data-reader-jump]");
      if (!jump) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      const url = jump.getAttribute("data-reader-jump");
      if (!url) return;
      const next =
        (RENDERED || []).find((m) => m.url === url) ||
        (STATE.mixed || []).find((m) => m.url === url) || {};
      document.querySelector("#reader .reader-card")?.scrollTo({ top: 0 });
      openReader(url, next);
    });

  document.querySelector("#reader .reader-close")?.addEventListener("click", closeReader);
  document.querySelector("#reader .reader-backdrop")?.addEventListener("click", closeReader);
  // Delete modal — close, reason picker, and ESC.
  document.querySelectorAll('#delete-modal [data-close="delete"]').forEach((n) =>
    n.addEventListener("click", closeDeleteModal));
  document.getElementById("delete-reasons")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".del-reason");
    if (!btn) return;
    confirmDelete(btn.dataset.reason || "other");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeReader(); closeDeleteModal(); closePwModal(); }
  });

  load();

  // ── Smart refresh ──────────────────────────────────────────────
  // Rules:
  //   - Only refresh while the tab is visible.
  //   - The interval is user-tunable from the settings popover —
  //     0 (OFF), 60 s, 120 s, 300 s (default), 600 s. Stored under
  //     dailybrief.refreshSeconds, read fresh on every probe so the
  //     setting takes effect immediately, no page reload needed.
  //   - When the tab returns to visible after being hidden longer than
  //     the configured interval, pull immediately.
  function refreshIntervalSeconds() {
    try {
      const v = parseInt(
        localStorage.getItem("dailybrief.refreshSeconds") || "1800", 10,
      );
      if (isNaN(v) || v < 0) return 300;
      return v;
    } catch { return 300; }
  }
  function smartCheck() {
    const interval = refreshIntervalSeconds();
    if (interval === 0) return;          // user disabled auto-refresh
    if (document.hidden) return;
    if (!LAST_LOAD) return;
    if (Date.now() - LAST_LOAD < interval * 1000) return;
    silentRefresh();
  }
  setInterval(smartCheck, 30 * 1000);  // probe every 30 s, cheap noop
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    const interval = refreshIntervalSeconds();
    if (interval === 0) return;
    if (!LAST_LOAD || Date.now() - LAST_LOAD > interval * 1000) {
      silentRefresh();
    }
  });

  // Tick the "updated Xm ago" label every 30s so it stays accurate
  // between refreshes.
  setInterval(() => {
    if (!LAST_LOAD) return;
    const status = $("#status");
    if (status && status.textContent.includes("updated")) {
      status.textContent = status.textContent.replace(
        /updated [^·]+$/,
        `updated ${fmtAge(LAST_LOAD)}`
      );
    }
  }, 30 * 1000);
});

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

// ── Strip renderers (whales / trades / videos) ────────────────
// Each strip mimics the price tape: a horizontal marquee that loops.
// Clicking any item opens the flow modal (see openFlow). Items are
// duplicated into two blocks for seamless scroll.
function _renderStrip(rootId, label, items, makeItem, kind) {
  const root = $(`#${rootId}`);
  if (!root) return;
  root.innerHTML = "";
  if (!items || !items.length) return;
  const track = el("div", { class: "strip-track" });
  const block = () => {
    const frag = document.createDocumentFragment();
    frag.appendChild(el("span", { class: "strip-label" }, label));
    items.forEach((it, idx) => {
      const node = makeItem(it);
      node.classList.add("strip-item");
      node.dataset.kind = kind;
      node.dataset.index = String(idx);
      frag.appendChild(node);
    });
    return frag;
  };
  track.appendChild(block());
  track.appendChild(block());
  root.appendChild(track);
}

// Known centralized exchanges. Movement TO one = sell pressure
// (the holder is depositing to sell); movement FROM one = buy pressure
// (the holder is withdrawing to hold long).
const EXCHANGES = [
  "binance", "coinbase", "kraken", "bitfinex", "bitstamp",
  "okx", "bybit", "kucoin", "huobi", "gate.io", "mexc",
  "upbit", "bithumb",
];
function isExchange(label) {
  if (!label) return false;
  const l = label.toLowerCase();
  return EXCHANGES.some((e) => l.includes(e));
}
function shortWallet(label) {
  if (!label) return "—";
  const l = label.toLowerCase();
  if (l.includes("unknown wallet") || l.includes("unknown")) return "UNK";
  if (l.includes("tether treasury")) return "Tether";
  if (l.includes("treasury")) return "Treasury";
  // strip trailing " wallet"
  return label.replace(/\s*wallet\s*$/i, "").trim();
}
function whaleDirection(w) {
  const fromX = isExchange(w.from_label);
  const toX   = isExchange(w.to_label);
  if (toX && !fromX) return "SELL";  // deposit → likely sell pressure
  if (fromX && !toX) return "BUY";   // withdraw → likely buy / accumulation
  return null;                       // wallet ↔ wallet, or exchange ↔ exchange
}

function renderWhalesStrip(items) {
  _renderStrip("strip-whales", "🐋 WHALES", items, (w) => {
    const dir = whaleDirection(w);
    return el("span", {}, [
      el("span", { class: "asset" }, w.asset || ""),
      dir ? el("span", { class: `action ${dir}` }, dir) : null,
      el("span", { class: "amt" }, fmtUSD(w.amount_usd || 0)),
      el("span", { class: "flow" }, shortWallet(w.from_label)),
      el("span", { class: "arrow" }, "→"),
      el("span", { class: "flow" }, shortWallet(w.to_label)),
    ]);
  }, "whale");
}

function renderTradesStrip(items) {
  _renderStrip("strip-trades", "🏛 INSIDERS", items, (t) => el("span", {}, [
    el("span", { class: "who", lang: "ko" }, t.name || ""),
    el("span", { class: `action ${t.action}` }, t.action || ""),
    el("span", { class: "ticker" }, t.ticker || ""),
    el("span", { class: "band" }, t.size_band || ""),
  ]), "trade");
}

function renderVideosStrip(items) {
  _renderStrip("strip-videos", "📺 WATCH", items, (v) => el("span", {}, [
    el("span", { class: "channel" }, v.channel || ""),
    el("span", { class: "vtitle" }, v.title || ""),
  ]), "video");
}

// ── News card body ─────────────────────────────────────────────
function newsBody(item, opts = {}) {
  // When the card is in its "translated view" we swap title + dek for
  // the saved Korean versions and re-tag the meta line as KO so
  // Pretendard kicks in for the headline.
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
  const why = item.why ? el("div", { class: "why", lang }, item.why) : null;
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

// Set of article URLs currently rendered in their Korean-translated
// view. Survives only the browser tab; persisted to localStorage so
// pagination + chip clicks keep the toggle state.
const CARD_KO_KEY = "dailybrief.cardKo.v1";
let CARD_KO_URLS = new Set();
try {
  const raw = localStorage.getItem(CARD_KO_KEY);
  if (raw) CARD_KO_URLS = new Set(JSON.parse(raw));
} catch {}
function _persistCardKo() {
  try { localStorage.setItem(CARD_KO_KEY, JSON.stringify([...CARD_KO_URLS])); } catch {}
}

// Mark a single item in every in-memory list (wire payload + DB-served
// page override) as freshly translated. Used right after a successful
// reader-modal /api/translate response so the main-feed card gains
// the ✦한 badge + neon border without waiting for the next refresh.
function markItemTranslated(url, td) {
  if (!url || !td) return;
  const dek = (td.paragraphs && td.paragraphs[0]) || "";
  const title_ko = td.title || "";
  const translated_at = Math.floor(Date.now() / 1000);
  const apply = (list) => {
    if (!Array.isArray(list)) return;
    for (const m of list) {
      if (m && m.url === url) {
        m.title_ko = title_ko;
        m.dek_ko = dek.slice(0, 280);
        m.translated_at = translated_at;
      }
    }
  };
  apply(STATE.mixed);
  apply(PAGE_OVERRIDE);
}

// Surgical re-render: swap a single .art element in place, preserving
// its scroll position + leaving every other card untouched. Replaces
// the old "paint(false) → rebuild everything" path that flashed the
// whole page when toggling one card's translation.
function rerenderCard(url) {
  if (!url) return false;
  const oldNode = [...document.querySelectorAll(".art")]
    .find((n) => n.getAttribute("data-url") === url);
  if (!oldNode) return false;
  const item =
    (STATE.mixed || []).find((m) => m.url === url) ||
    (PAGE_OVERRIDE || []).find((m) => m.url === url);
  if (!item) return false;
  // Preserve whatever tier the card is currently in.
  const tierMatch = oldNode.className.match(/\btier-(\w+)\b/);
  const tier = tierMatch ? tierMatch[1] : "small";
  const fresh = renderNewsCard(item, tier);
  // Suppress the fade-in animation on a re-render — fade is meant
  // for first paint, not for in-place edits.
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
  // ✦한 / ✦EN badge floats over the image. Only rendered if a
  // translation exists for this URL.
  let koBadge = null;
  if (item.title_ko) {
    koBadge = el("button", {
      class: "ko-badge",
      type: "button",
      "data-ko-toggle": "1",
      title: "AI 번역 보기 / 원문 보기",
      "aria-label": "translate toggle",
    }, [
      el("span", { class: "ko-spark", "aria-hidden": "true" }, "✦"),
      el("span", { class: "ko-text", lang: "ko" }, inKo ? "원문" : "한"),
    ]);
  }

  if (imgUrl) {
    const imgWrap = el("div", { class: "img-wrap" });
    const imgEl = el("div", { class: "img", style: `background-image:url('${imgUrl}')` });
    imgWrap.appendChild(imgEl);
    if (koBadge) imgWrap.appendChild(koBadge);
    if (tier === "hero") {
      node.appendChild(imgWrap);
      node.appendChild(el("div", { class: "body" }, [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow]));
    } else {
      node.appendChild(imgWrap);
      [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    }
  } else {
    [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    // No image? Put the badge inline with the meta line.
    if (koBadge) {
      koBadge.classList.add("ko-badge-inline");
      parts.meta.appendChild(koBadge);
    }
  }
  return node;
}

// ── Score-based tier assignment ────────────────────────────────
// Image-bearing items (real photo or backend AI-image) fill HERO →
// FEATURE → LARGE → MEDIUM → SMALL. Items that are missing both fall
// to HEADLINE/FLASH text tiers, which live in the float section.
function buildPage(news) {
  const sorted = [...news].sort((a, b) => b.score - a.score);
  const out = [];
  let usedHero = false;
  const budget = { feature: 2, large: 4, medium: 8 };

  for (const item of sorted) {
    const hasImage = !!pickImage(item);
    let tier;
    if (hasImage) {
      if (!usedHero) { tier = "hero"; usedHero = true; }
      else if (budget.feature > 0)     { tier = "feature"; budget.feature--; }
      else if (budget.large > 0)       { tier = "large";   budget.large--; }
      else if (budget.medium > 0)      { tier = "medium";  budget.medium--; }
      else                             { tier = "small"; }
    } else {
      tier = item.score >= 70 ? "headline" : "flash";
    }
    out.push({ item, tier });
  }
  return out;
}

// ── State ──────────────────────────────────────────────────────
let STATE = { mixed: [], tape: [], whales: [], trades: [], youtube: [] };
let CAT = "all";
let PAGE = 1;
let LAST_LOAD = null;
let PENDING_REFRESH = false;  // set true when silentRefresh is deferred
// Odd page size so HERO (always 1) + the rest is even, which packs
// cleanly into the 2-per-row tiers below the hero.
const PAGE_SIZE = 31;
const AUTO_REFRESH_MS = 2 * 60 * 1000;  // 2 minutes — near-real-time

function filteredNews() {
  return STATE.mixed.filter((it) => {
    if (it.kind !== "news") return false;
    if (CAT !== "all" && it.category !== CAT) return false;
    return true;
  });
}

// Lazy DB-backed pagination: page 1 comes from the in-memory feed
// (fast, freshly curated). Pages 2+ are pulled from /api/page so the
// total page count is unbounded — it grows with the archive.
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
  // Use the DB-served override when present (page 2+, or any category
  // filter). Otherwise use the in-memory mixed slice.
  if (PAGE_OVERRIDE && PAGE_OVERRIDE_N === PAGE) {
    slice = PAGE_OVERRIDE;
  } else {
    if (PAGE > memPages) PAGE = memPages;   // safety while async fetch finishes
    const startIdx = (PAGE - 1) * PAGE_SIZE;
    slice = all.slice(startIdx, startIdx + PAGE_SIZE);
  }

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
  if (totalPages <= 1) return;
  const go = async (n) => {
    PAGE = Math.max(1, Math.min(totalPages, n));
    await fetchPage(PAGE);
    paint();
  };
  // Minimal pager — was a 104-button avalanche. Now: « PREV |
  // page N of T | NEXT ». Scroll-back via PREV, forward via NEXT,
  // and a current-page indicator in the middle. The clickable current
  // chip lets the user jump straight back to page 1 from anywhere
  // by tapping it (acts as "home" on long sessions).
  const prev = el("button", { type: "button", class: "pg-arrow" }, "‹ PREV");
  prev.disabled = PAGE === 1;
  prev.onclick = () => go(PAGE - 1);
  p.appendChild(prev);

  const indicator = el("button",
    { type: "button", class: "pg-current", title: "tap to jump to page 1" },
    `${PAGE} / ${totalPages}`);
  indicator.onclick = () => go(1);
  p.appendChild(indicator);

  const next = el("button", { type: "button", class: "pg-arrow" }, "NEXT ›");
  next.disabled = PAGE === totalPages;
  next.onclick = () => go(PAGE + 1);
  p.appendChild(next);
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
  renderWhalesStrip(STATE.whales || []);
  renderTradesStrip(STATE.trades || []);
  renderVideosStrip(STATE.youtube || []);
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
  const flowOpen = !document.getElementById("flow")?.classList.contains("hidden");
  if (readerOpen || flowOpen) {
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

// ── Reader modal ───────────────────────────────────────────────
// State the translate button consults: which article is currently
// open, its original payload, its translated payload (if any), and
// which view is showing.
let READER_STATE = {
  url: null,
  item: null,
  original: null,
  translated: null,
  view: "original",   // "original" | "translated"
};

async function openReader(url, item, opts = {}) {
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
    url, item: item || {}, original: null, translated: null,
    view: opts.initialKo ? "translated" : "original",
  };
  updateTranslateButton();

  try {
    const r = await fetch(`/api/article?url=${encodeURIComponent(url)}`);
    const data = await r.json();
    if (data.error) {
      content.innerHTML =
        `<div class="reader-loading">⚠ ${data.error}<br><br>` +
        `<a class="reader-original" href="${url}" target="_blank" rel="noopener">open original ↗</a></div>`;
      return;
    }
    READER_STATE.original = data;
    // If the card was in KO state when clicked, jump straight to the
    // translated view. The translation is usually already cached in
    // SQLite (since the card badge only appears when a translation
    // exists), so this is a near-instant DB lookup.
    if (opts.initialKo) {
      try {
        const tr = await fetch(
          `/api/translate?url=${encodeURIComponent(url)}&lang=ko`,
        );
        const td = await tr.json();
        if (!td.error && td.paragraphs) {
          READER_STATE.translated = td;
          READER_STATE.view = "translated";
          updateTranslateButton();
          renderReader(content, td, item || {});
          return;
        }
      } catch {}
      // Translation fetch failed — fall through to showing the original.
    }
    updateTranslateButton();
    renderReader(content, data, item || {});
  } catch (e) {
    content.innerHTML = `<div class="reader-loading">error: ${e.message}</div>`;
  }
}

// Decide the target language: if the article is English, go to KR.
// If Korean, go to EN. Otherwise, default to KR.
function targetLangFor(srcLang) {
  if (srcLang === "ko") return "en";
  return "ko";
}

function relocateTranslateButton(metaEl) {
  // The button lives in the static index.html under .reader-card so
  // there's only ever one of them. After the meta line renders we
  // pluck it out and re-append into the meta strip — that way it
  // shows up next to WORDS / DATE instead of floating on the photo.
  const btn = document.getElementById("reader-translate");
  if (!btn || !metaEl) return;
  metaEl.appendChild(btn);
}

function updateTranslateButton() {
  const btn = document.getElementById("reader-translate");
  if (!btn) return;
  const data = READER_STATE.original;
  const label = btn.querySelector(".rt-label");

  // Hide the button entirely until we know the source language AND
  // the source is NOT already Korean. User explicitly: no KO→EN button.
  if (!data) {
    btn.hidden = true;
    btn.classList.remove("active", "loading");
    return;
  }
  const srcLang = (data.lang || "en").toLowerCase();
  if (srcLang === "ko") {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;

  if (label) {
    // Label tells the user what the click WILL do.
    if (READER_STATE.view === "translated") {
      label.textContent = "원문";       // "original" — flip back
      label.setAttribute("lang", "ko");
    } else {
      label.textContent = "번역";       // "translate" — go to KR
      label.setAttribute("lang", "ko");
    }
  }
  btn.classList.toggle("active", READER_STATE.view === "translated");
}

async function toggleTranslation() {
  const btn = document.getElementById("reader-translate");
  if (!btn || !READER_STATE.original) return;
  const content = document.querySelector("#reader .reader-content");
  // If we already have a translation, just flip the view.
  if (READER_STATE.translated && READER_STATE.view === "original") {
    READER_STATE.view = "translated";
    renderReader(content, READER_STATE.translated, READER_STATE.item);
    updateTranslateButton();
    return;
  }
  if (READER_STATE.view === "translated") {
    READER_STATE.view = "original";
    renderReader(content, READER_STATE.original, READER_STATE.item);
    updateTranslateButton();
    return;
  }
  // First-time translate: fire the API.
  btn.classList.add("loading");
  try {
    const tgt = targetLangFor(READER_STATE.original.lang || "en");
    const r = await fetch(
      `/api/translate?url=${encodeURIComponent(READER_STATE.url)}&lang=${tgt}`,
    );
    const data = await r.json();
    if (data.error || !data.paragraphs) {
      btn.classList.remove("loading");
      btn.title = data.error || "translation failed";
      // Briefly flash the button red — no toast framework here.
      btn.style.borderColor = "var(--down)";
      setTimeout(() => { btn.style.borderColor = ""; }, 1500);
      return;
    }
    READER_STATE.translated = data;
    READER_STATE.view = "translated";
    renderReader(content, data, READER_STATE.item);
    updateTranslateButton();
    // Live-update the underlying feed card so the ✦한 badge + neon
    // border show up the instant the translation lands — no /api/brief
    // refresh needed. Persist the toggle state too so closing the
    // reader leaves the card in its Korean view.
    markItemTranslated(READER_STATE.url, data);
    CARD_KO_URLS.add(READER_STATE.url);
    _persistCardKo();
    rerenderCard(READER_STATE.url);
  } catch (e) {
    console.warn("translate failed:", e);
  } finally {
    btn.classList.remove("loading");
  }
}

function renderReader(content, data, item) {
  const lang = data.lang || item.lang || "en";
  content.innerHTML = "";   // the static .reader-close button lives OUTSIDE
                            // .reader-content, so this clears only the body.

  const imgSrc = data.image || item.image;
  if (imgSrc) {
    content.appendChild(el("div", {
      class: "reader-img",
      style: `background-image:url('${imgSrc}')`,
    }));
  }

  // Build the meta strip. Word-count + formal date inherit the same
  // mono-uppercase 10.5px / 0.8px letter-spacing from .reader-meta.
  // The ✦번역 translate pill (was floating on the photo) gets moved
  // INTO the meta line by JS — see relocateTranslateButton().
  const dateLabel = fmtFormal(item.ts);
  const metaEl = el("div", { class: "reader-meta" }, [
    item.outlet ? el("span", { class: "src" }, item.outlet) : null,
    item.category ? el("span", { class: "tag" }, item.category.toUpperCase()) : null,
    data.byline ? el("span", {}, data.byline) : null,
    data.word_count ? el("span", { class: "reader-stats" }, `${data.word_count} WORDS`) : null,
    dateLabel ? el("span", { class: "reader-stats" }, dateLabel) : null,
  ]);
  content.appendChild(metaEl);
  // Pull the translate button out of the modal card root and append
  // it to the meta line. updateTranslateButton() decides hidden/shown.
  relocateTranslateButton(metaEl);

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

  content.appendChild(el("div", { class: "reader-footer" }, [
    el("span", { class: "badge" }, "✦ dailybrief reader"),
    el("a", {
      href: data.url || item.url,
      target: "_blank",
      rel: "noopener",
      class: "reader-original",
    }, "open original ↗"),
  ]));
}

function closeReader() {
  document.getElementById("reader").classList.add("hidden");
  unlockBodyScroll();
  // Reset translation state so the next article starts fresh.
  READER_STATE = { url: null, item: null, original: null, translated: null, view: "original" };
  const btn = document.getElementById("reader-translate");
  if (btn) {
    btn.classList.remove("active", "loading");
    btn.style.borderColor = "";
  }
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
function openFlow(kind, index) {
  const modal = document.getElementById("flow");
  if (!modal) return;
  lockBodyScroll();
  const list =
    kind === "whale" ? (STATE.whales || []) :
    kind === "trade" ? (STATE.trades || []) :
    kind === "video" ? (STATE.youtube || []) : [];
  const item = list[index];
  if (!item) return;
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  // lockBodyScroll() above already handles overflow; no extra style nudge.
  const content = document.getElementById("flow-content");
  content.innerHTML = "";

  if (kind === "whale") {
    content.appendChild(el("div", { class: "reader-meta" }, [
      el("span", { class: "tag" }, "WHALE"),
      el("span", { class: "src" }, item.asset || ""),
      el("span", {}, fmtWhen(item.timestamp)),
    ]));
    content.appendChild(el("h1", { class: "reader-title" },
      `${fmtUSD(item.amount_usd || 0)} ${item.asset || ""}`));
    content.appendChild(el("div", { class: "reader-body" }, [
      el("p", {}, `From: ${item.from_label || "—"}`),
      el("p", {}, `To: ${item.to_label || "—"}`),
    ]));
    content.appendChild(el("div", { class: "reader-footer" }, [
      el("span", { class: "badge" }, "✦ whale-alert"),
      item.tx_url ? el("a", {
        href: item.tx_url, target: "_blank", rel: "noopener",
        class: "reader-original",
      }, "transaction ↗") : null,
    ]));
  } else if (kind === "trade") {
    content.appendChild(el("div", { class: "reader-meta" }, [
      el("span", { class: "tag" }, "INSIDER TRADE"),
      el("span", { class: "src" }, item.role || ""),
      el("span", {}, fmtWhen(item.timestamp)),
    ]));
    content.appendChild(el("h1", { class: "reader-title", lang: "ko" },
      `${item.name} ${item.action} ${item.ticker}`));
    content.appendChild(el("div", { class: "reader-body" }, [
      el("p", {}, `Company: ${item.company || item.ticker}`),
      el("p", {}, `Size band: ${item.size_band || "—"}`),
    ]));
    content.appendChild(el("div", { class: "reader-footer" }, [
      el("span", { class: "badge" }, "✦ insider trades"),
      item.source_url ? el("a", {
        href: item.source_url, target: "_blank", rel: "noopener",
        class: "reader-original",
      }, "disclosure ↗") : null,
    ]));
  } else if (kind === "video") {
    content.appendChild(el("div", { class: "reader-meta" }, [
      el("span", { class: "tag" }, "VIDEO"),
      el("span", { class: "src" }, item.channel || ""),
      el("span", {}, fmtWhen(item.published)),
    ]));
    if (item.thumbnail) {
      content.appendChild(el("div", {
        class: "reader-img",
        style: `background-image:url('${item.thumbnail}')`,
      }));
    }
    content.appendChild(el("h1", { class: "reader-title" }, item.title || ""));
    content.appendChild(el("div", { class: "reader-footer" }, [
      el("span", { class: "badge" }, "✦ youtube"),
      item.url ? el("a", {
        href: item.url, target: "_blank", rel: "noopener",
        class: "reader-original",
      }, "watch on YouTube ↗") : null,
    ]));
  }
}

function closeFlow() {
  const modal = document.getElementById("flow");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  unlockBodyScroll();
  if (PENDING_REFRESH) {
    PENDING_REFRESH = false;
    setTimeout(silentRefresh, 250);
  }
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
  let atTop = false;
  let atBottom = false;
  let armed = false;
  // Slightly more eager arming + lower dismiss threshold so the
  // swipe-down-at-top gesture actually feels responsive. Article is
  // open → finger touches → drag down → release; ~75px ends the modal.
  const ARM_PX = 4;
  const DISMISS_DELTA = 75;

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
    atTop = card.scrollTop <= 0;
    atBottom = card.scrollTop + card.clientHeight >= card.scrollHeight - 1;
    armed = false;
  }, { passive: true });

  card.addEventListener("touchmove", (e) => {
    if (startY === null) return;
    lastY = e.touches[0].clientY;
    const rawDelta = lastY - startY;

    if (!armed) {
      if (rawDelta > ARM_PX && atTop) armed = true;
      else if (rawDelta < -ARM_PX && atBottom) armed = true;
      else return;
    }

    const d = damp(rawDelta);
    card.style.transition = "none";
    card.style.willChange = "transform, opacity";
    // Subtle scale + fade as the user drags further — gives "weight"
    const progress = Math.min(1, Math.abs(rawDelta) / 320);
    const scale = 1 - progress * 0.05;          // shrinks toward 0.95
    const opacity = 1 - progress * 0.25;        // fades toward 0.75
    card.style.transform = `translateY(${d}px) scale(${scale})`;
    card.style.opacity = String(opacity);
    const backdrop = modal.querySelector(".reader-backdrop");
    if (backdrop) backdrop.style.opacity = String(Math.max(0.1, 1 - progress * 0.85));
  }, { passive: true });

  card.addEventListener("touchend", () => {
    if (startY === null) return;
    if (!armed) { startY = null; lastY = null; return; }

    const rawDelta = (lastY ?? startY) - startY;
    const backdrop = modal.querySelector(".reader-backdrop");

    if (Math.abs(rawDelta) > DISMISS_DELTA) {
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
  });
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
}

// ── Wire up ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  }).toUpperCase();

  // Swipe gestures on the reader + flow modals (mobile dismiss UX)
  attachSwipeToClose("#reader", closeReader);
  attachSwipeToClose("#flow",   closeFlow);

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
  $("#text-scale-segs")?.addEventListener("click", (e) => {
    const seg = e.target.closest(".seg");
    if (!seg) return;
    applyTextScale(parseFloat(seg.dataset.scale));
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

  $("#chips").addEventListener("click", async (e) => {
    // The ▾ expand toggle lives inside the nav but isn't a chip.
    if (e.target.closest("#chip-toggle")) return;
    const chip = e.target.closest(".chip");
    if (!chip) return;
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
    // Card-level KO toggle has priority — clicking the badge must not
    // also open the reader.
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
      // Surgical swap — only this one card re-renders, no page-wide flash.
      if (!rerenderCard(url)) {
        // Fallback if the card couldn't be located (rare).
        paint(false);
      }
      return;
    }
    const art = e.target.closest("#paper .art, #paper-noimg .art");
    if (!art) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    const url = art.getAttribute("href");
    const item = STATE.mixed.find((m) => m.url === url) || {};
    // If the card is currently in its translated view, open the reader
    // with the translated body already showing.
    const openInKo = CARD_KO_URLS.has(url) && !!item.title_ko;
    openReader(url, item, { initialKo: openInKo });
  });

  // Close-on-button + backdrop + Escape for both modals. The static
  // ×/backdrop in index.html are each wired ONCE here.
  document.querySelector("#reader .reader-close")?.addEventListener("click", closeReader);
  document.querySelector("#reader .reader-backdrop")?.addEventListener("click", closeReader);
  // Translation toggle (local-only feature) — single click, switches
  // the article body between original and AI-translated views.
  document.getElementById("reader-translate")?.addEventListener("click", toggleTranslation);
  document.querySelectorAll('#flow [data-close="flow"]').forEach((n) =>
    n.addEventListener("click", closeFlow));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeReader(); closeFlow(); }
  });

  // Strip clicks → flow modal. data-kind + data-index are stamped onto
  // each .strip-item by _renderStrip.
  document.addEventListener("click", (e) => {
    const item = e.target.closest(".strip-item");
    if (!item) return;
    const kind = item.dataset.kind;
    const idx = parseInt(item.dataset.index || "0", 10);
    if (kind) openFlow(kind, idx);
  });

  load();

  // ── Smart refresh ──────────────────────────────────────────────
  // No user toggle, no fixed timer. Rules:
  //   - Only refresh while the tab is visible.
  //   - When the tab returns to visible and it's been > 3 min since
  //     the last successful refresh, pull immediately.
  //   - While visible, top up every 5 minutes (matches the backend
  //     brief-cache TTL ≫ 2 min so we don't pay for repeated LLM
  //     curation cycles).
  const VISIBLE_INTERVAL_MS = 5 * 60 * 1000;
  const VISIBILITY_STALE_MS = 3 * 60 * 1000;
  function smartCheck() {
    if (document.hidden) return;
    if (!LAST_LOAD) return;
    if (Date.now() - LAST_LOAD < VISIBLE_INTERVAL_MS) return;
    silentRefresh();
  }
  setInterval(smartCheck, 60 * 1000);  // probe every 60 s, cheap noop
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (!LAST_LOAD || Date.now() - LAST_LOAD > VISIBILITY_STALE_MS) {
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

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

// More natural "when" display: "오늘 14:32" / "어제 09:11" / "May 28"
function fmtPub(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yest = new Date(now); yest.setDate(now.getDate() - 1);
  const isYest = d.toDateString() === yest.toDateString();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (sameDay)  return `${hh}:${mm}`;
  if (isYest)   return `Y · ${hh}:${mm}`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
function newsBody(item) {
  const lang = item.lang || "en";
  const meta = el("div", { class: "meta" }, [
    el("span", { class: "tag" }, (item.category || "news").toUpperCase()),
    el("span", { class: "src" }, item.outlet || ""),
    el("span", { class: `lang lang-${lang}` }, lang.toUpperCase()),
    el("span", { class: "when" }, fmtPub(item.ts)),
    item.score != null ? el("span", { class: "score-pill" }, `★${item.score}`) : null,
  ]);
  const head = el("h2", { class: "h", lang }, decodeEntities(item.title) || "");
  const dek = item.dek ? el("p", { class: "dek", lang }, decodeEntities(item.dek)) : null;
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

function renderNewsCard(item, tier) {
  const parts = newsBody(item);
  const cat = item.category || "world";
  const node = el("a", {
    class: `art tier-${tier} cat-${cat}`,
    href: item.url || "#",
    target: "_blank",
    rel: "noopener",
  });

  const wantsImage = ["hero", "feature", "large", "medium", "small"].includes(tier);
  const imgUrl = wantsImage ? pickImage(item) : null;
  if (imgUrl) {
    const imgEl = el("div", { class: "img", style: `background-image:url('${imgUrl}')` });
    if (tier === "hero") {
      node.appendChild(imgEl);
      node.appendChild(el("div", { class: "body" }, [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow]));
    } else {
      node.appendChild(imgEl);
      [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    }
  } else {
    [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
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

function fmtAge(ts) {
  if (!ts) return "—";
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 60)        return `${diff}s ago`;
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function paint(scrollTop = true) {
  const all = filteredNews();
  const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  if (PAGE > totalPages) PAGE = totalPages;
  const startIdx = (PAGE - 1) * PAGE_SIZE;
  const slice = all.slice(startIdx, startIdx + PAGE_SIZE);

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

  // chip counts (from full STATE, not filtered)
  const counts = { all: 0, world: 0, econ: 0, tech: 0, ai: 0, crypto: 0, korea: 0 };
  STATE.mixed.forEach((m) => {
    if (m.kind !== "news") return;
    counts.all++;
    if (counts[m.category] != null) counts[m.category]++;
  });
  Object.entries(counts).forEach(([k, v]) => {
    const n = document.getElementById(`ct-${k}`);
    if (n) n.textContent = v;
  });

  renderPager(totalPages);

  const outlets = Object.keys(STATE.by_outlet || {}).length;
  $("#status").textContent = `${all.length} news · page ${PAGE}/${totalPages} · ${outlets} sources · updated ${fmtAge(LAST_LOAD)}`;
  if (scrollTop) window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderPager(totalPages) {
  const p = $("#pager");
  p.innerHTML = "";
  if (totalPages <= 1) return;
  const go = (n) => { PAGE = Math.max(1, Math.min(totalPages, n)); paint(); };
  const prev = el("button", { type: "button" }, "‹ PREV");
  prev.disabled = PAGE === 1;
  prev.onclick = () => go(PAGE - 1);
  p.appendChild(prev);
  const radius = 3;
  const lo = Math.max(1, PAGE - radius);
  const hi = Math.min(totalPages, PAGE + radius);
  if (lo > 1) {
    const b = el("button", { type: "button" }, "1");
    b.onclick = () => go(1);
    p.appendChild(b);
    if (lo > 2) p.appendChild(el("span", { class: "label" }, "…"));
  }
  for (let i = lo; i <= hi; i++) {
    const b = el("button", { type: "button" }, String(i));
    if (i === PAGE) b.classList.add("current");
    b.onclick = () => go(i);
    p.appendChild(b);
  }
  if (hi < totalPages) {
    if (hi < totalPages - 1) p.appendChild(el("span", { class: "label" }, "…"));
    const b = el("button", { type: "button" }, String(totalPages));
    b.onclick = () => go(totalPages);
    p.appendChild(b);
  }
  const next = el("button", { type: "button" }, "NEXT ›");
  next.disabled = PAGE === totalPages;
  next.onclick = () => go(PAGE + 1);
  p.appendChild(next);
}

// ── Load ───────────────────────────────────────────────────────
async function load() {
  $("#status").textContent = "loading…";
  try {
    const r = await fetch("/api/brief");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    STATE = await r.json();
    STATE.mixed.forEach((m) => { if (m.dek) m.dek = stripHtml(m.dek); });

    renderTape(STATE.tape || []);
    const head = $("#headline");
    head.textContent = STATE.headline || "";
    head.lang = (STATE.profile && STATE.profile.primary_lang) || "en";

    renderWhalesStrip(STATE.whales || []);
    renderTradesStrip(STATE.trades || []);
    renderVideosStrip(STATE.youtube || []);

    LAST_LOAD = Date.now();
    PAGE = 1;
    paint();
  } catch (e) {
    $("#status").textContent = `error: ${e.message}`;
  }
}

// Silent auto-refresh: pull /api/brief, swap data, redraw current page
// without resetting pagination or scrolling. Triggered on a timer.
async function silentRefresh() {
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
    renderWhalesStrip(STATE.whales || []);
    renderTradesStrip(STATE.trades || []);
    renderVideosStrip(STATE.youtube || []);
    paint(false);  // preserve page + scroll
  } catch (e) {
    console.warn("auto-refresh failed:", e);
  }
}

// ── Reader modal ───────────────────────────────────────────────
async function openReader(url, item) {
  const modal = document.getElementById("reader");
  if (!modal) return;
  modal.classList.remove("hidden");
  // Hard-lock the page behind so touch-drag / scroll never leaks through.
  document.body.style.overflow = "hidden";
  document.body.style.position = "fixed";
  document.body.style.left = "0";
  document.body.style.right = "0";
  document.body.dataset.scrollY = String(window.scrollY);
  document.body.style.top = `-${window.scrollY}px`;
  const content = modal.querySelector(".reader-content");
  content.innerHTML = `<div class="reader-loading">📖 READING…</div>`;

  try {
    const r = await fetch(`/api/article?url=${encodeURIComponent(url)}`);
    const data = await r.json();
    if (data.error) {
      content.innerHTML =
        `<div class="reader-loading">⚠ ${data.error}<br><br>` +
        `<a class="reader-original" href="${url}" target="_blank" rel="noopener">open original ↗</a></div>`;
      return;
    }
    renderReader(content, data, item || {});
  } catch (e) {
    content.innerHTML = `<div class="reader-loading">error: ${e.message}</div>`;
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

  content.appendChild(el("div", { class: "reader-meta" }, [
    item.outlet ? el("span", { class: "src" }, item.outlet) : null,
    item.category ? el("span", { class: "tag" }, item.category.toUpperCase()) : null,
    data.byline ? el("span", {}, data.byline) : null,
    item.ts ? el("span", {}, fmtWhen(item.ts)) : null,
    data.word_count ? el("span", {}, `${data.word_count} words`) : null,
  ]));

  content.appendChild(el("h1", { class: "reader-title", lang },
    data.title || item.title || "(no title)"));

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
  document.body.style.overflow = "";
}

// ── Flow detail modal (whales / trades / videos) ───────────────
function lockBodyScroll() {
  document.body.style.overflow = "hidden";
  document.body.style.position = "fixed";
  document.body.style.left = "0";
  document.body.style.right = "0";
  document.body.dataset.scrollY = String(window.scrollY);
  document.body.style.top = `-${window.scrollY}px`;
}
function unlockBodyScroll() {
  const y = parseInt(document.body.dataset.scrollY || "0", 10);
  document.body.style.overflow = "";
  document.body.style.position = "";
  document.body.style.top = "";
  document.body.style.left = "";
  document.body.style.right = "";
  window.scrollTo(0, y);
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
  document.body.style.overflow = "hidden";
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
  const ARM_PX = 6;
  const DISMISS_DELTA = 110;

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
  $("#settings-refresh")?.addEventListener("click", async () => {
    toggleSettings(false);
    await fetch("/api/refresh", { method: "POST" });
    await load();
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

  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    CAT = chip.dataset.cat;
    PAGE = 1;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    paint();
  });
  // Intercept article-card clicks → open the reader modal.
  // ⌘/Ctrl/Shift/middle-click keeps the default behaviour (new tab).
  document.addEventListener("click", (e) => {
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
  document.querySelector("#reader .reader-close")?.addEventListener("click", closeReader);
  document.querySelector("#reader .reader-backdrop")?.addEventListener("click", closeReader);
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

  // Auto-refresh every 5 minutes — quietly pulls the freshest data and
  // updates the page without disturbing the user's pagination or scroll.
  setInterval(silentRefresh, AUTO_REFRESH_MS);

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

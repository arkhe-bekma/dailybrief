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

// ── Right rail renderers ───────────────────────────────────────
function renderWhalesRail(items) {
  const root = $("#rail-whales");
  root.innerHTML = "";
  items.slice(0, 12).forEach((w) => {
    root.appendChild(el("a", {
      class: "w-row",
      href: w.tx_url || "#",
      target: "_blank",
      rel: "noopener",
    }, [
      el("span", { class: "amt" }, fmtUSD(w.amount_usd || 0)),
      el("span", { class: "asset" }, w.asset || ""),
      el("span", { class: "flow" }, `${w.from_label} → ${w.to_label}`),
      el("span", { class: "when" }, fmtWhen(w.timestamp)),
    ]));
  });
}

function renderTradesRail(items) {
  const root = $("#rail-trades");
  root.innerHTML = "";
  items.slice(0, 14).forEach((t) => {
    root.appendChild(el("a", {
      class: "t-row",
      href: t.source_url || "#",
      target: "_blank",
      rel: "noopener",
    }, [
      el("span", { class: "who", lang: "ko" }, t.name || ""),
      el("span", { class: `action ${t.action}` }, t.action || ""),
      el("span", { class: "meta-line" }, [
        el("span", { class: "ticker" }, t.ticker || ""),
        el("span", {}, t.size_band || ""),
        el("span", {}, fmtWhen(t.timestamp)),
      ]),
    ]));
  });
}

function renderVideosRail(items) {
  const root = $("#rail-videos");
  root.innerHTML = "";
  items.slice(0, 14).forEach((v) => {
    root.appendChild(el("a", {
      class: "v-row",
      href: v.url || "#",
      target: "_blank",
      rel: "noopener",
    }, [
      el("span", { class: "thumb", style: `background-image:url('${v.thumbnail}')` }),
      el("span", { class: "meta-block" }, [
        el("span", { class: "ch" }, v.channel || ""),
        el("span", { class: "title" }, v.title || ""),
      ]),
    ]));
  });
}

// ── News card body ─────────────────────────────────────────────
function newsBody(item) {
  const lang = item.lang || "en";
  const meta = el("div", { class: "meta" }, [
    el("span", { class: "tag" }, (item.category || "news").toUpperCase()),
    el("span", { class: "src" }, item.outlet || ""),
    el("span", { class: `lang lang-${lang}` }, lang.toUpperCase()),
    el("span", { class: "when" }, fmtWhen(item.ts)),
    item.score != null ? el("span", { class: "score-pill" }, `★${item.score}`) : null,
  ]);
  const head = el("h2", { class: "h", lang }, item.title || "");
  const dek = item.dek ? el("p", { class: "dek", lang }, item.dek) : null;
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
const PAGE_SIZE = 30;
const AUTO_REFRESH_MS = 5 * 60 * 1000;  // 5 minutes

function filteredNews() {
  const q = $("#filter").value.trim().toLowerCase();
  return STATE.mixed.filter((it) => {
    if (it.kind !== "news") return false;
    if (CAT !== "all" && it.category !== CAT) return false;
    if (!q) return true;
    const hay = `${it.title || ""} ${it.outlet || ""} ${(it.tickers||[]).join(" ")}`.toLowerCase();
    return hay.includes(q);
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

    renderWhalesRail(STATE.whales || []);
    renderTradesRail(STATE.trades || []);
    renderVideosRail(STATE.youtube || []);

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
    renderWhalesRail(STATE.whales || []);
    renderTradesRail(STATE.trades || []);
    renderVideosRail(STATE.youtube || []);
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
  document.body.style.overflow = "hidden";
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
  content.innerHTML = "";

  // Close button stays in the DOM
  const closeBtn = el("button", { class: "reader-close", "aria-label": "close" }, "×");
  closeBtn.addEventListener("click", closeReader);
  content.appendChild(closeBtn);

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

  if (data.tldr) {
    content.appendChild(el("div", { class: "reader-tldr" }, [
      el("span", { class: "tldr-label" }, "✦ TL;DR"),
      el("p", { lang }, data.tldr),
    ]));
  }

  if (data.key_points && data.key_points.length) {
    content.appendChild(el("span", { class: "reader-section-label" }, "→ KEY POINTS"));
    const ul = el("ul", { class: "reader-points" });
    data.key_points.forEach((p) => ul.appendChild(el("li", { lang }, p)));
    content.appendChild(ul);
  }

  if (data.paragraphs && data.paragraphs.length) {
    content.appendChild(el("span", { class: "reader-section-label" }, "✦ THE STORY"));
    const body = el("div", { class: "reader-body" });
    data.paragraphs.forEach((p) => body.appendChild(el("p", { lang }, p)));
    content.appendChild(body);
  }

  content.appendChild(el("div", { class: "reader-footer" }, [
    el("span", { class: "badge" }, "✦ AI SUMMARY · dailybrief"),
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

// ── Wire up ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
  }).toUpperCase();

  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    CAT = chip.dataset.cat;
    PAGE = 1;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    paint();
  });
  $("#filter").addEventListener("input", () => { PAGE = 1; paint(); });
  $("#refresh").addEventListener("click", async () => {
    await fetch("/api/refresh", { method: "POST" });
    await load();
  });
  $("#density").addEventListener("click", () => {
    $("#paper").classList.toggle("dense");
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

  // Close-on-backdrop + Escape
  document.querySelector(".reader-backdrop")?.addEventListener("click", closeReader);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeReader();
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

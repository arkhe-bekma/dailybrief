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
  if (wantsImage && item.image) {
    if (tier === "hero") {
      node.appendChild(el("div", { class: "img", style: `background-image:url('${item.image}')` }));
      node.appendChild(el("div", { class: "body" }, [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow]));
    } else {
      node.appendChild(el("div", { class: "img", style: `background-image:url('${item.image}')` }));
      [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
    }
  } else {
    // No image — use one of the text tiers
    [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow].forEach((p) => p && node.appendChild(p));
  }
  return node;
}

// ── Score-based tier assignment ────────────────────────────────
// Goal: visually weight by importance, never waste vertical space on
// image-less items. We allow at most one HERO per page.
function buildPage(news) {
  // sort by score desc (already sorted by mixer, but be defensive)
  const sorted = [...news].sort((a, b) => b.score - a.score);
  const out = [];
  let usedHero = false;
  let imageBudget = { feature: 2, large: 4, medium: 8, small: Infinity };

  for (const item of sorted) {
    const hasImage = !!item.image;
    let tier;
    const s = item.score;

    if (hasImage) {
      if (!usedHero && s >= 80) {
        tier = "hero";
        usedHero = true;
      } else if (imageBudget.feature > 0 && s >= 75) {
        tier = "feature"; imageBudget.feature--;
      } else if (imageBudget.large > 0 && s >= 65) {
        tier = "large"; imageBudget.large--;
      } else if (imageBudget.medium > 0) {
        tier = "medium"; imageBudget.medium--;
      } else {
        tier = "small";
      }
    } else {
      tier = s >= 70 ? "headline" : "flash";
    }
    out.push({ item, tier });
  }
  return out;
}

// ── State ──────────────────────────────────────────────────────
let STATE = { mixed: [], tape: [], whales: [], trades: [], youtube: [] };
let CAT = "all";
let PAGE = 1;
const PAGE_SIZE = 30;

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

function paint() {
  const all = filteredNews();
  const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  if (PAGE > totalPages) PAGE = totalPages;
  const startIdx = (PAGE - 1) * PAGE_SIZE;
  const slice = all.slice(startIdx, startIdx + PAGE_SIZE);

  const paper = $("#paper");
  paper.innerHTML = "";
  buildPage(slice).forEach(({ item, tier }) => {
    paper.appendChild(renderNewsCard(item, tier));
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
  $("#status").textContent = `${all.length} news · page ${PAGE}/${totalPages} · ${outlets} sources · ${STATE.whales.length}🐋 ${STATE.trades.length}🏛 ${STATE.youtube.length}📺`;
  window.scrollTo({ top: 0, behavior: "smooth" });

  // After the grid lays itself out, fill any vertical slack by scaling
  // the title up. Two RAFs because: 1st RAF paints, 2nd RAF reads final
  // post-layout heights.
  requestAnimationFrame(() => requestAnimationFrame(autoFitAllTitles));
}

// ── Auto-fit titles: scale up the headline to fill any leftover
//    vertical space inside the card. Only applies to text-only tiers
//    (HEADLINE / FLASH / SMALL) where grid stretching creates gaps.
function autoFitAllTitles() {
  document.querySelectorAll("#paper .art").forEach(autoFitCard);
}

function autoFitCard(card) {
  if (!card.matches(".tier-headline, .tier-flash, .tier-small")) return;
  const title = card.querySelector(".h");
  if (!title) return;

  // Reset to CSS-driven baseline
  title.style.fontSize = "";
  title.style.lineHeight = "";

  const baseSize = parseFloat(getComputedStyle(title).fontSize);
  if (!baseSize) return;

  const cardH = card.clientHeight;
  if (cardH < 60) return;

  const cs = getComputedStyle(card);
  const padTop = parseFloat(cs.paddingTop)    || 0;
  const padBot = parseFloat(cs.paddingBottom) || 0;
  const rowGap = parseFloat(cs.rowGap || cs.gap || 0) || 0;

  // Sum siblings + gaps between them inside .art
  const children = Array.from(card.children);
  const others = children.filter((c) => c !== title);
  const siblingHeights = others.reduce((sum, c) => sum + c.offsetHeight, 0);
  const gaps = children.length > 1 ? rowGap * (children.length - 1) : 0;

  const available = cardH - padTop - padBot - siblingHeights - gaps;
  if (available <= title.offsetHeight + 8) return;  // no meaningful slack

  // Binary search for the largest font-size that still fits in `available`.
  const cap = baseSize * 2.4;        // never more than 2.4× the baseline
  let lo = baseSize, hi = cap;
  for (let i = 0; i < 9; i++) {
    const mid = (lo + hi) / 2;
    title.style.fontSize = `${mid}px`;
    if (title.offsetHeight <= available) lo = mid;
    else hi = mid;
  }
  title.style.fontSize = `${Math.max(baseSize, lo - 1)}px`;
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

    PAGE = 1;
    paint();
  } catch (e) {
    $("#status").textContent = `error: ${e.message}`;
  }
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

  // Re-run auto-fit on window resize (debounced)
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(autoFitAllTitles, 120);
  });

  load();
});

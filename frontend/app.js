const $ = (s) => document.querySelector(s);

// ── Helpers ────────────────────────────────────────────────────
const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)        return "just now";
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
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
function sparkSVG(values, w = 48, h = 16, strokeOverride = null) {
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
  const color = strokeOverride || (up ? "var(--up)" : "var(--down)");
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const poly = document.createElementNS(NS, "polyline");
  poly.setAttribute("points", pts);
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", color);
  poly.setAttribute("stroke-width", "1.6");
  poly.setAttribute("stroke-linejoin", "round");
  poly.setAttribute("stroke-linecap", "round");
  // path length for the draw animation
  poly.setAttribute("stroke-dasharray", "200");
  svg.appendChild(poly);
  return svg;
}

// ── Ticker tape ────────────────────────────────────────────────
function renderTape(quotes) {
  const tape = $("#tape");
  tape.innerHTML = "";
  if (!quotes || !quotes.length) return;
  const inner = document.createElement("div");
  inner.className = "tape-track";
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
  inner.appendChild(buildBlock());
  inner.appendChild(buildBlock());
  tape.appendChild(inner);
}

// ── Article card body builders ─────────────────────────────────
function newsBody(item) {
  const lang = item.lang || "en";
  const meta = el("div", { class: "meta" }, [
    el("span", { class: "cat-tag" }, (item.category || "news").toUpperCase()),
    el("span", { class: "src" }, item.outlet || ""),
    el("span", { class: `lang lang-${lang}` }, lang.toUpperCase()),
    el("span", { class: "when" }, fmtWhen(item.ts)),
  ]);
  const head = el("h2", { class: "h", lang }, item.title || "");
  const dek = item.dek ? el("p", { class: "dek", lang }, item.dek) : null;
  const why = item.why ? el("div", { class: "why", lang }, item.why) : null;

  // sparkline tickers
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

function whaleBody(item) {
  return {
    meta: el("div", { class: "meta" }, [
      el("span", { class: "cat-tag" }, "WHALE"),
      el("span", { class: "src" }, item.asset || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    head: el("div", {}, [
      el("div", { class: "amount" }, fmtUSD(item.amount_usd || 0)),
      el("div", { class: "flow" }, item.title || ""),
    ]),
  };
}

function tradeBody(item) {
  return {
    meta: el("div", { class: "meta" }, [
      el("span", { class: "cat-tag" }, "TRADE"),
      el("span", { class: "src" }, item.role || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    head: el("div", {}, [
      el("div", { class: "who" }, item.title.split("·")[0]),
      el("div", { class: "row" }, [
        el("span", { class: `action ${item.action}` }, item.action || ""),
        el("span", { class: "ticker" }, item.ticker || ""),
        el("span", { class: "band" }, item.size_band || ""),
      ]),
    ]),
  };
}

function videoBody(item) {
  return {
    meta: el("div", { class: "meta" }, [
      el("span", { class: "cat-tag" }, "VIDEO"),
      el("span", { class: "src" }, item.channel || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    head: el("h2", { class: "h" }, item.title || ""),
  };
}

const BODY_BUILDERS = {
  news: newsBody, whale: whaleBody, trade: tradeBody, video: videoBody,
};

// ── Render one article card given a tier ───────────────────────
function renderArt(item, tier) {
  const builder = BODY_BUILDERS[item.kind] || newsBody;
  const parts = builder(item);
  const cat = item.kind === "news" ? (item.category || "world") : item.kind;
  const node = el("a", {
    class: `art tier-${tier} kind-${item.kind} cat-${cat}`,
    href: item.url || "#",
    target: "_blank",
    rel: "noopener",
  });

  const hasImage = tier === "hero" || tier === "lead" || tier === "standard";
  if (hasImage && item.image) {
    const img = el("div", { class: "img", style: `background-image:url('${item.image}')` });
    if (tier === "hero") {
      node.appendChild(img);
      const body = el("div", { class: "body" }, [parts.meta, parts.head, parts.dek, parts.why, parts.sparkRow]);
      node.appendChild(body);
    } else {
      node.appendChild(img);
      node.appendChild(parts.meta);
      node.appendChild(parts.head);
      if (parts.dek) node.appendChild(parts.dek);
      if (parts.why) node.appendChild(parts.why);
      if (parts.sparkRow) node.appendChild(parts.sparkRow);
    }
  } else {
    // No image → text-only
    node.appendChild(parts.meta);
    node.appendChild(parts.head);
    if (parts.dek) node.appendChild(parts.dek);
    if (parts.why) node.appendChild(parts.why);
    if (parts.sparkRow) node.appendChild(parts.sparkRow);
  }
  return node;
}

// ── State ──────────────────────────────────────────────────────
let STATE = { mixed: [], tape: [], headline: null };
let CAT = "all";
let PAGE = 1;
const PAGE_SIZE = 30;   // 1 hero + 4 lead + 9 standard + 16 compact/tiny ≈ 30

function filtered() {
  const q = $("#filter").value.trim().toLowerCase();
  return STATE.mixed.filter((it) => {
    if (CAT === "hero") return it.score >= 70;
    if (CAT !== "all") {
      // map category → either it.kind (whale/trade/video) or it.category (world/econ/...)
      if (["whale", "trade", "video"].includes(CAT)) return it.kind === CAT;
      return it.kind === "news" && it.category === CAT;
    }
    if (!q) return true;
    const hay = `${it.title || ""} ${it.outlet || ""} ${it.channel || ""} ${it.ticker || ""} ${it.asset || ""} ${(it.tickers||[]).join(" ")}`.toLowerCase();
    return hay.includes(q);
  });
}

// ── Tier assignment ────────────────────────────────────────────
function assignTier(idx, item, hasImage) {
  if (idx === 0)          return "hero";
  if (idx <= 4)           return hasImage ? "lead" : "compact";
  if (idx <= 13)          return hasImage ? "standard" : "compact";
  if (idx <= 21)          return "compact";
  return "tiny";
}

// ── Paint ──────────────────────────────────────────────────────
function paint() {
  const all = filtered();
  const totalPages = Math.max(1, Math.ceil(all.length / PAGE_SIZE));
  if (PAGE > totalPages) PAGE = totalPages;
  const startIdx = (PAGE - 1) * PAGE_SIZE;
  const slice = all.slice(startIdx, startIdx + PAGE_SIZE);

  const paper = $("#paper");
  paper.innerHTML = "";
  slice.forEach((item, idx) => {
    const hasImage = !!item.image && item.kind !== "trade" && item.kind !== "whale";
    const tier = assignTier(idx, item, hasImage);
    paper.appendChild(renderArt(item, tier));
  });

  // chip counts
  const cats = ["all","world","econ","tech","ai","crypto","korea","whale","trade","video"];
  const counts = Object.fromEntries(cats.map((c) => [c, 0]));
  STATE.mixed.forEach((m) => {
    counts.all += 1;
    if (["whale","trade","video"].includes(m.kind)) counts[m.kind]++;
    else if (m.kind === "news" && m.category && counts[m.category] != null) counts[m.category]++;
  });
  Object.entries(counts).forEach(([k, v]) => {
    const n = document.getElementById(`ct-${k}`);
    if (n) n.textContent = v;
  });

  // pager
  renderPager(totalPages);

  // status
  const outlets = Object.keys(STATE.by_outlet || {}).length;
  $("#status").textContent = `${all.length} items · page ${PAGE}/${totalPages} · ${outlets} outlets`;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderPager(totalPages) {
  const p = $("#pager");
  p.innerHTML = "";
  if (totalPages <= 1) return;
  const go = (n) => { PAGE = Math.max(1, Math.min(totalPages, n)); paint(); };
  const prev = el("button", { type: "button" }, "‹ prev");
  prev.disabled = PAGE === 1;
  prev.onclick = () => go(PAGE - 1);
  p.appendChild(prev);
  // show up to 7 page numbers around current
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
  const next = el("button", { type: "button" }, "next ›");
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
    // strip HTML out of summaries for dek
    STATE.mixed.forEach((m) => {
      if (m.dek) m.dek = stripHtml(m.dek);
    });
    renderTape(STATE.tape || []);
    const head = $("#headline");
    head.textContent = STATE.headline || "";
    head.lang = (STATE.profile && STATE.profile.primary_lang) || "en";
    PAGE = 1;
    paint();
  } catch (e) {
    $("#status").textContent = `error: ${e.message}`;
  }
}

// ── Wire up ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
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

  $("#filter").addEventListener("input", () => { PAGE = 1; paint(); });
  $("#refresh").addEventListener("click", async () => {
    await fetch("/api/refresh", { method: "POST" });
    await load();
  });
  $("#density").addEventListener("click", () => {
    $("#paper").classList.toggle("dense");
  });
  load();
});

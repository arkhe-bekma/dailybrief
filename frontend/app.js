const $ = (s) => document.querySelector(s);

const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60)      return "just now";
  if (diff < 3600)    return `${Math.floor(diff / 60)}m`;
  if (diff < 86400)   return `${Math.floor(diff / 3600)}h`;
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
  if (p >= 100)   return p.toFixed(2);
  if (p >= 1)     return p.toFixed(2);
  return p.toFixed(4);
};

const fmtPct = (p) => `${p >= 0 ? "+" : ""}${p.toFixed(2)}%`;

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

// ── SVG sparkline ──────────────────────────────────────────────
function sparkSVG(values, w = 48, h = 16, stroke = null) {
  if (!values || values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = w / (values.length - 1);
  const pts = values.map((v, i) => {
    const x = i * stepX;
    const y = h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const trend = values[values.length - 1] >= values[0];
  const color = stroke || (trend ? "var(--up)" : "var(--down)");
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const poly = document.createElementNS(svgNS, "polyline");
  poly.setAttribute("points", pts);
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", color);
  poly.setAttribute("stroke-width", "1.4");
  poly.setAttribute("stroke-linejoin", "round");
  poly.setAttribute("stroke-linecap", "round");
  svg.appendChild(poly);
  return svg;
}

// ── Tape ───────────────────────────────────────────────────────
function renderTape(quotes) {
  const tape = $("#tape");
  tape.innerHTML = "";
  if (!quotes || !quotes.length) return;
  // Render twice for seamless marquee loop.
  const inner = document.createElement("div");
  inner.className = "tape-track";
  const block = () => {
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
  inner.appendChild(block());
  inner.appendChild(block());
  tape.appendChild(inner);
}

// ── Tile renderers ─────────────────────────────────────────────
function renderNews(item) {
  const lang = item.lang || "en";
  const parts = [
    el("div", { class: "head" }, [
      el("span", { class: "kind" }, "NEWS"),
      el("span", { class: "src" }, item.outlet || ""),
      item.category ? el("span", {}, item.category) : null,
      el("span", { class: `lang lang-${lang}` }, lang.toUpperCase()),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    el("div", { class: "title", lang }, item.title || ""),
    item.why ? el("div", { class: "why", lang }, item.why) : null,
  ];

  // Sparkline row
  const sparks = item.sparks || {};
  const tickerList = (item.tickers || []).filter((t) => sparks[t]);
  if (tickerList.length) {
    const row = el("div", { class: "spark-row" }, tickerList.map((t) => {
      const sv = sparkSVG(sparks[t]);
      const trend = sparks[t][sparks[t].length - 1] - sparks[t][0];
      const pct = (trend / sparks[t][0]) * 100;
      const upDown = pct >= 0 ? "up" : "down";
      const node = el("span", { class: "spark" }, [
        el("span", { class: "sym" }, t.replace("-USD", "").replace(".KS", "")),
        sv,
        el("span", { class: `ch ${upDown}`, style: `color: var(--${upDown});` }, fmtPct(pct)),
      ]);
      return node;
    }));
    parts.push(row);
  }

  if (item.image) {
    parts.push(el("div", { class: "thumb", style: `background-image:url('${item.image}')` }));
  }
  return parts;
}

function renderWhale(item) {
  return [
    el("div", { class: "head" }, [
      el("span", { class: "kind" }, "WHALE"),
      el("span", { class: "src" }, item.asset || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    el("div", { class: "amount" }, fmtUSD(item.amount_usd || 0)),
    el("div", { class: "title" }, item.title || ""),
  ];
}

function renderTrade(item) {
  return [
    el("div", { class: "head" }, [
      el("span", { class: "kind" }, "TRADE"),
      el("span", { class: "src" }, item.role || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    el("div", { class: "badge" }, [
      el("span", { class: `action ${item.action}` }, item.action || ""),
      el("span", { class: "ticker" }, item.ticker || ""),
      el("span", { class: "band" }, `· ${item.size_band || ""}`),
    ]),
    el("div", { class: "title" }, item.title || ""),
  ];
}

function renderVideo(item) {
  return [
    el("div", { class: "head" }, [
      el("span", { class: "kind" }, "VIDEO"),
      el("span", { class: "src" }, item.channel || ""),
      el("span", { class: "when" }, fmtWhen(item.ts)),
    ]),
    el("div", { class: "title" }, item.title || ""),
    item.image ? el("div", { class: "thumb", style: `background-image:url('${item.image}')` }) : null,
  ];
}

const RENDERERS = {
  news: renderNews, whale: renderWhale, trade: renderTrade, video: renderVideo,
};

function tileFor(item) {
  const render = RENDERERS[item.kind] || renderNews;
  const children = render(item).concat(
    el("span", { class: "score" }, String(item.score ?? ""))
  );
  return el("a", {
    class: `tile ${item.kind}`,
    href: item.url || "#",
    target: "_blank",
    rel: "noopener",
  }, children);
}

// ── State + paint ──────────────────────────────────────────────
let STATE = { mixed: [], tape: [], headline: null };
let FILTER_KIND = "all";

function paint() {
  const wall = $("#wall");
  wall.innerHTML = "";
  const q = $("#filter").value.trim().toLowerCase();

  const items = STATE.mixed.filter((it) => {
    if (FILTER_KIND !== "all" && it.kind !== FILTER_KIND) return false;
    if (!q) return true;
    const hay = `${it.title || ""} ${it.outlet || ""} ${it.channel || ""} ${it.ticker || ""} ${it.asset || ""} ${(it.tickers||[]).join(" ")}`.toLowerCase();
    return hay.includes(q);
  });
  items.forEach((it) => wall.appendChild(tileFor(it)));

  const counts = { all: STATE.mixed.length, news: 0, whale: 0, trade: 0, video: 0 };
  STATE.mixed.forEach((it) => { counts[it.kind] = (counts[it.kind] || 0) + 1; });
  Object.entries(counts).forEach(([k, v]) => {
    const node = document.getElementById(`ct-${k}`);
    if (node) node.textContent = v;
  });
}

// ── Load ───────────────────────────────────────────────────────
async function load() {
  $("#status").textContent = "loading…";
  try {
    const r = await fetch("/api/brief");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    STATE = await r.json();
    renderTape(STATE.tape || []);
    const head = $("#headline");
    head.textContent = STATE.headline || "";
    head.lang = (STATE.profile && STATE.profile.primary_lang) || "en";
    paint();
    const outlets = Object.keys(STATE.by_outlet || {}).length;
    $("#status").textContent = `${STATE.mixed.length} items · ${outlets} outlets · ${(STATE.tape||[]).length} tickers`;
  } catch (e) {
    $("#status").textContent = `error: ${e.message}`;
  }
}

// ── Wire up ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#today").textContent = new Date().toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
  });

  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    FILTER_KIND = chip.dataset.kind;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    paint();
  });

  $("#filter").addEventListener("input", paint);

  $("#refresh").addEventListener("click", async () => {
    await fetch("/api/refresh", { method: "POST" });
    await load();
  });

  $("#density").addEventListener("click", () => {
    $("#wall").classList.toggle("dense");
  });

  load();
});

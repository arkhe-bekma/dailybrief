/* Status page. Polls /api/health/extraction and renders it.
   Deliberately dependency-free and defensive: this is the page people
   open when something is already wrong, so it must render even if the
   API is the thing that's broken. */

const $ = (id) => document.getElementById(id);

let windowHours = 24;
let timer = null;

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (v === null || v === undefined) return;
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  });
  (Array.isArray(children) ? children : [children])
    .filter((c) => c !== null && c !== undefined)
    .forEach((c) => node.appendChild(
      typeof c === "string" ? document.createTextNode(c) : c,
    ));
  return node;
}

// "3m ago" / "4h ago" / "2d ago". Unix seconds in, human string out.
function ago(tsSeconds) {
  if (!tsSeconds) return "never";
  const secs = Math.max(0, Math.floor(Date.now() / 1000) - tsSeconds);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function rateClass(rate) {
  if (rate >= 0.8) return "rate-ok";
  if (rate >= 0.5) return "rate-warn";
  return "rate-bad";
}

function renderBanner(data) {
  const banner = $("banner");
  const text = $("banner-text");
  const degraded = data.degraded || [];
  const subscription = data.subscription || [];
  banner.dataset.state = data.status || "ok";

  if (!data.attempts) {
    banner.dataset.state = "ok";
    text.textContent = "No article reads recorded in this window yet.";
    return;
  }
  // Subscriber-only outlets are expected to fall back, so they're
  // mentioned but never drive the banner colour — a permanently red
  // page is a page nobody reads.
  const subNote = subscription.length
    ? ` ${subscription.length} subscriber-only source` +
      `${subscription.length === 1 ? " is" : "s are"} being dropped, as expected.`
    : "";

  if (!degraded.length) {
    const pct = Math.round((data.success_rate || 0) * 100);
    text.textContent =
      `All sources working — ${pct}% of articles opened with the full body.` + subNote;
  } else {
    const names = degraded.slice(0, 3).map((o) => o.outlet).join(", ");
    const more = degraded.length > 3 ? ` +${degraded.length - 3} more` : "";
    text.textContent =
      `${degraded.length} source${degraded.length === 1 ? "" : "s"} degraded ` +
      `(${names}${more}).` + subNote;
  }
}

function renderSummary(data) {
  const pct = data.success_rate === null || data.success_rate === undefined
    ? null : Math.round(data.success_rate * 100);
  const tone = pct === null ? "" : pct >= 80 ? "ok" : pct >= 50 ? "warn" : "bad";
  const tiles = [
    ["Full articles", String(data.ok ?? 0), "ok"],
    ["Dropped, no body", String(data.failed ?? 0), (data.failed ? "warn" : "")],
    ["Success rate", pct === null ? "—" : `${pct}%`, tone],
    ["Sources seen", String((data.outlets || []).length), ""],
  ];
  $("summary").replaceChildren(...tiles.map(([k, v, cls]) =>
    el("div", { class: "st-card" }, [
      el("span", { class: "k" }, k),
      el("span", { class: `v ${cls}`.trim() }, v),
    ])));
}

function renderOutlets(data) {
  const rows = (data.outlets || []).slice().sort((a, b) => {
    // Worst-performing first — the whole point of the page is to surface
    // what's broken, so healthy sources sink to the bottom.
    if (a.success_rate !== b.success_rate) return a.success_rate - b.success_rate;
    return b.attempts - a.attempts;
  });
  $("outlet-empty").hidden = rows.length > 0;
  $("outlet-rows").replaceChildren(...rows.map((o) => {
    const pct = Math.round(o.success_rate * 100);
    return el("tr", {}, [
      el("td", { class: "outlet" }, o.outlet),
      el("td", { class: "num" }, String(o.ok)),
      el("td", { class: "num" }, String(o.failed)),
      el("td", { class: `num ${rateClass(o.success_rate)}` }, [
        el("span", { class: "st-rate" }, [
          el("span", { class: "pct" }, `${pct}%`),
          el("span", { class: "st-bar" }, el("i", { style: `width:${pct}%` })),
        ]),
      ]),
      el("td", {}, [
        o.reason
          ? el("span", { class: `st-tag tag-${o.reason}` }, o.reason)
          : null,
        el("span", { class: "ago" }, ago(o.last_ok)),
      ]),
    ]);
  }));
}

function renderReasons(data) {
  const reasons = data.reasons || {};
  const keys = Object.keys(reasons);
  if (!keys.length) {
    $("reasons").replaceChildren(
      el("span", { class: "st-reason none" }, "Nothing was dropped in this window."));
    return;
  }
  $("reasons").replaceChildren(...keys.map((k) =>
    el("span", { class: "st-reason" }, [
      el("b", {}, String(reasons[k])),
      el("span", {}, k),
    ])));
}

async function load() {
  try {
    const r = await fetch(`/api/health/extraction?hours=${windowHours}`, {
      cache: "no-store",
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderBanner(data);
    renderSummary(data);
    renderOutlets(data);
    renderReasons(data);
    $("updated").textContent =
      `updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (err) {
    $("banner").dataset.state = "error";
    $("banner-text").textContent =
      `Couldn't reach the status API (${err.message}). The app itself may be down.`;
  }
}

$("range").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-h]");
  if (!btn) return;
  windowHours = Number(btn.dataset.h);
  [...$("range").querySelectorAll("button")]
    .forEach((b) => b.classList.toggle("on", b === btn));
  load();
});

load();
timer = setInterval(load, 30000);
// Don't keep polling a tab nobody is looking at.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(timer);
  } else {
    load();
    timer = setInterval(load, 30000);
  }
});

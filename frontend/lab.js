// dailybrief LAB — pulls /api/lab and renders the dashboard. Polls
// every 4 seconds while the tab is foreground.

const $ = (s) => document.querySelector(s);
const POLL_MS = 4_000;

function fmt(n) {
  if (typeof n !== "number") return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtBytes(n) {
  if (!n) return "—";
  if (n > 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n > 1e6) return (n / 1e6).toFixed(2) + " MB";
  if (n > 1e3) return (n / 1e3).toFixed(1) + " kB";
  return n + " B";
}
function fmtAgo(epoch) {
  if (!epoch) return "—";
  const diff = Math.floor(Date.now() / 1000 - epoch);
  if (diff < 60)        return `${diff}s ago`;
  if (diff < 3600)      return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)     return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
function fmtInterval(s) {
  if (!s) return "—";
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0)   return `${s / 60} min`;
  return `${s} s`;
}

let stale = false;

async function tick() {
  try {
    const r = await fetch("/api/lab");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    paintOverview(d);
    $("#lab-live").classList.remove("stale");
    stale = false;
  } catch (e) {
    $("#lab-live").classList.add("stale");
    stale = true;
  }
  try {
    const r2 = await fetch("/api/lab/agent-runs");
    if (r2.ok) paintRuns((await r2.json()).runs || []);
  } catch {}
}

function paintOverview(d) {
  const db = d.db || {};
  const cfg = d.config || {};
  const cache = d.cache || {};

  $("#kpi-articles-total").textContent = fmt(db.articles_total);
  $("#kpi-articles-24h").textContent   = fmt(db.articles_24h);
  $("#kpi-outlets").textContent        = fmt(d.outlets_configured);
  $("#kpi-db-size").textContent        = fmtBytes(db.db_bytes);

  $("#kpi-llm-on").textContent      = cfg.anthropic_key_set ? "YES" : "NO";
  $("#kpi-hits-mem").textContent    = fmt((db.counters || {})["reader_cache_hits_mem"] || 0);
  $("#kpi-hits-disk").textContent   = fmt((db.counters || {})["reader_cache_hits_disk"] || 0);
  $("#kpi-extracts-ok").textContent = fmt((db.counters || {})["reader_extracts_ok"] || 0);

  $("#kpi-interval").textContent    = fmtInterval(cfg.agent_interval_seconds);
  $("#kpi-cache-total").textContent = fmt(cache.total_keys);
  const inp = $("#interval-input");
  if (document.activeElement !== inp) inp.value = cfg.agent_interval_seconds || 3600;

  // Outlet roster
  const tbody = $("#outlet-table tbody");
  const outlets = (db.outlets || []).slice(0, 30);
  const total = outlets.reduce((s, o) => s + (o.articles || 0), 0) || 1;
  tbody.innerHTML = outlets.map((o) => `
    <tr>
      <td>${o.outlet || "—"}</td>
      <td class="num">${o.articles || 0}</td>
      <td class="ago">${fmtAgo(o.last_fetched)}</td>
      <td class="num">${((o.articles || 0) * 100 / total).toFixed(1)}%</td>
    </tr>
  `).join("") || `<tr><td colspan="4" class="ago">no articles yet — waiting on first /api/brief</td></tr>`;

  // Cache prefix bars
  const bars = $("#cache-bars");
  const entries = Object.entries(cache.by_prefix || {});
  const max = Math.max(1, ...entries.map(([, n]) => n));
  bars.innerHTML = entries.map(([k, n]) => `
    <div class="bar-row">
      <span class="bar-label">${k}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${(n / max * 100).toFixed(1)}%"></span></span>
      <span class="bar-n">${fmt(n)}</span>
    </div>
  `).join("") || `<span class="ago">cache empty</span>`;

  // Raw counters
  const cnts = Object.entries(db.counters || {});
  $("#counter-list").innerHTML = cnts.length ? cnts.map(([k, v]) => `
    <div class="kpi"><span class="kpi-label">${k.toUpperCase()}</span><span class="kpi-v">${fmt(v)}</span></div>
  `).join("") : `<span class="ago">no counters yet</span>`;
}

function paintRuns(runs) {
  const tbody = $("#runs-table tbody");
  tbody.innerHTML = runs.map((r) => {
    const dur = r.ended_at && r.started_at ? `${r.ended_at - r.started_at}s` : "—";
    return `<tr>
      <td class="ago">${fmtAgo(r.started_at)}</td>
      <td>${r.kind}</td>
      <td class="${r.ok ? "ok" : "err"}">${r.ok ? "ok" : "FAIL"}</td>
      <td class="num">${dur}</td>
      <td class="ago">${r.note || ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="5" class="ago">no agent runs logged yet</td></tr>`;
}

$("#interval-save").addEventListener("click", async () => {
  const v = parseInt($("#interval-input").value, 10);
  if (!Number.isFinite(v)) return;
  try {
    await fetch("/api/lab/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_interval_seconds: v }),
    });
    $("#lab-status").textContent = `interval saved: ${v} s`;
    setTimeout(() => { $("#lab-status").textContent = `refreshing every ${POLL_MS/1000}s`; }, 2200);
    tick();
  } catch (e) {
    $("#lab-status").textContent = `save failed: ${e.message}`;
  }
});

tick();
setInterval(() => { if (!document.hidden) tick(); }, POLL_MS);

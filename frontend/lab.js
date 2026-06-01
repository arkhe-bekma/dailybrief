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
  try {
    const r3 = await fetch("/api/lab/agents");
    if (r3.ok) paintAgents(await r3.json());
  } catch {}
  try {
    const r4 = await fetch("/api/ingest/status");
    if (r4.ok) paintIngest(await r4.json());
  } catch {}
  try {
    const r5 = await fetch("/api/storage");
    if (r5.ok) paintStorage(await r5.json());
  } catch {}
}

function paintIngest(d) {
  const elPass = document.getElementById("ingest-pass");
  const elFail = document.getElementById("ingest-fail");
  const elPend = document.getElementById("ingest-pending");
  const elSum  = document.getElementById("ingest-summary");
  const elBar  = document.getElementById("ingest-bar-fill");
  const elTxt  = document.getElementById("ingest-bar-text");
  const elReasons = document.getElementById("ingest-reasons");
  if (elPass) elPass.textContent = fmt(d.validated || 0);
  if (elFail) elFail.textContent = fmt(d.failed || 0);
  if (elPend) elPend.textContent = fmt(d.pending || 0);
  if (elSum)  elSum.textContent = `${d.validated || 0} pass · ${d.failed || 0} reject · ${d.pending || 0} pending`;
  if (elBar)  elBar.style.width = `${d.percent || 0}%`;
  if (elTxt)  elTxt.textContent = `${d.percent || 0}% checked (${d.done || 0}/${d.total || 0})`;
  if (elReasons) {
    const reasons = d.reasons || [];
    elReasons.innerHTML = reasons.length
      ? `<span class="ingest-reasons-label">TOP REJECTION REASONS</span>` +
        reasons.map((r) => `
          <span class="ingest-reason-pill" title="${r.reason}">
            <span class="ingest-reason-name">${r.reason || "—"}</span>
            <span class="ingest-reason-n">${r.n}</span>
          </span>
        `).join("")
      : "";
  }
}

function paintStorage(d) {
  const set = (id, txt) => {
    const e = document.getElementById(id);
    if (e) e.textContent = txt;
  };
  set("kpi-db-size2", fmtBytes(d.db_bytes));
  set("kpi-disk-free", fmtBytes(d.disk_free));
  set("kpi-disk-used", `${d.disk_used_pct || 0}%`);
  set("kpi-rss",       fmtBytes(d.rss_bytes));
  set("kpi-cache2",    fmt(d.cache_keys || 0));

  // Alert when storage is tight
  const alert = document.getElementById("storage-alert");
  if (alert) {
    let msg = null;
    if (d.disk_free && d.disk_free < 1 * 1e9) {
      msg = `⚠ disk space low: ${fmtBytes(d.disk_free)} free`;
    } else if (d.disk_used_pct && d.disk_used_pct > 85) {
      msg = `⚠ disk ${d.disk_used_pct}% full`;
    } else if (d.rss_bytes && d.rss_bytes > 700 * 1e6) {
      msg = `⚠ uvicorn RSS ${fmtBytes(d.rss_bytes)}`;
    }
    if (msg) {
      alert.hidden = false;
      alert.textContent = msg;
    } else {
      alert.hidden = true;
    }
  }
}

function paintAgents(d) {
  const wfWrap = document.getElementById("workflow-list");
  const active = d.active_workflow;
  wfWrap.innerHTML = (d.workflows || []).map((w) => `
    <label class="wf-item ${w.key === active ? "wf-active" : ""}">
      <input type="radio" name="workflow" value="${w.key}" ${w.key === active ? "checked" : ""}/>
      <div class="wf-body">
        <div class="wf-head">
          <span class="wf-label">${w.label}</span>
          <span class="wf-pill">${(w.agents || []).length} AGENTS</span>
        </div>
        <div class="wf-desc">${w.description || ""}</div>
        <div class="wf-chips">${(w.agents || []).map((a) => `<span class="wf-chip">${a}</span>`).join("")}</div>
      </div>
    </label>
  `).join("");
  wfWrap.querySelectorAll('input[name="workflow"]').forEach((r) => {
    r.addEventListener("change", async () => {
      if (!r.checked) return;
      try {
        await fetch("/api/lab/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workflow: r.value }),
        });
        document.getElementById("lab-status").textContent = `workflow saved: ${r.value}`;
        setTimeout(() => {
          document.getElementById("lab-status").textContent = `refreshing every ${POLL_MS / 1000}s`;
        }, 2200);
        tick();
      } catch (e) {
        document.getElementById("lab-status").textContent = `save failed: ${e.message}`;
      }
    });
  });

  const agentsWrap = document.getElementById("agent-list");
  agentsWrap.innerHTML = (d.agents || []).map((a) => `
    <div class="agent-card ${a.always_on ? "always" : "optional"}">
      <div class="agent-head">
        <span class="agent-name">${a.name}</span>
        <span class="agent-status">${a.always_on ? "● ALWAYS ON" : "○ OPT-IN"}</span>
      </div>
      <div class="agent-role">${a.role}</div>
      <div class="agent-summary">${a.summary}</div>
      <div class="agent-file"><code>${a.file}</code></div>
    </div>
  `).join("");
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

  // Outlet roster — every configured outlet, sorted by article count.
  // Outlets with 0 articles are shown too so the user can spot the
  // ones whose feed isn't returning anything.
  const tbody = $("#outlet-table tbody");
  const outlets = db.outlets || [];
  const total = outlets.reduce((s, o) => s + (o.articles || 0), 0) || 1;
  const active = outlets.filter((o) => (o.articles || 0) > 0).length;
  const dead = outlets.filter((o) => o.configured && (o.articles || 0) === 0).length;
  const sumEl = $("#outlet-summary");
  if (sumEl) sumEl.textContent = `${outlets.length} configured · ${active} active · ${dead} dead`;

  tbody.innerHTML = outlets.map((o) => {
    const n = o.articles || 0;
    const share = ((n * 100) / total).toFixed(1);
    let status = "";
    if (!o.configured)            status = `<span class="badge-dead">RETIRED</span>`;
    else if (n === 0)             status = `<span class="badge-dead">NO ARTICLES</span>`;
    else if (o.premium)           status = `<span class="badge-prem">★ PREMIUM</span>`;
    else                          status = `<span class="badge-ok">OK</span>`;
    const lang = (o.lang || "?").toUpperCase();
    return `<tr class="${n === 0 ? "row-dead" : ""}">
      <td class="outlet-name" lang="${o.lang || "en"}">${o.outlet || "—"}</td>
      <td class="cat-tag">${(o.category || "—").toUpperCase()}</td>
      <td class="lang-tag lang-${o.lang || "en"}">${lang}</td>
      <td class="num">${n}</td>
      <td class="ago">${fmtAgo(o.last_fetched)}</td>
      <td class="num">${share}%</td>
      <td>${status}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="ago">no outlets configured</td></tr>`;

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

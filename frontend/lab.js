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
  try {
    const r6 = await fetch("/api/usage");
    if (r6.ok) paintUsage(await r6.json());
  } catch {}
}

// Friendly explanations for the validator rejection slugs — surfaced
// as title= tooltips on the lab pills so the user understands what
// each one means without spelunking through the code.
const REJECTION_HINTS = {
  "extract-failed":       "Couldn't fetch the article body (paywall, JS-only page, dead URL).",
  "too-few-paragraphs":   "Body has fewer than 2 paragraphs.",
  "no-image-and-short":   "No usable image AND the body is too short to stand alone.",
  "body-too-short":       "Body is shorter than 400 characters.",
  "title-too-short":      "Headline is shorter than 10 characters.",
  "title-echoed-in-body": "Body just repeats the title 2+ times — no real content.",
  "mostly-copyright":     "Most of the body is copyright/disclaimer boilerplate.",
  "paywall":              "Body looks like a paywall stub.",
  "series-filler":        "Photo-only / video-only / gallery teaser.",
  "no-body":              "Reader extraction returned nothing.",
  "no-url":               "Article has no URL — can't fetch.",
};

function _fmtCost(n) {
  if (!n) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1)    return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}
function _fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function paintUsage(d) {
  const today = d.today || {};
  const week  = d.week || {};
  const total = d.total || {};
  const set = (id, t) => { const e = document.getElementById(id); if (e) e.textContent = t; };

  set("usage-summary",      `today: ${_fmtCost(today.cost || 0)}`);
  set("usage-cost-today",   _fmtCost(today.cost || 0));
  set("usage-cost-week",    _fmtCost(week.cost  || 0));
  set("usage-cost-total",   _fmtCost(total.cost || 0));
  set("usage-detail-today",
    `${today.calls || 0} calls · ${_fmtTokens((today.in_tok || 0) + (today.out_tok || 0))} tok`);
  set("usage-detail-week",
    `${week.calls || 0} calls · ${_fmtTokens((week.in_tok || 0) + (week.out_tok || 0))} tok`);
  set("usage-detail-total",
    `${total.calls || 0} calls · ${_fmtTokens((total.in_tok || 0) + (total.out_tok || 0))} tok`);

  const provEl = document.getElementById("usage-by-provider");
  if (provEl) {
    const rows = d.by_provider_today || [];
    const max = Math.max(0.0001, ...rows.map((r) => r.cost || 0));
    provEl.innerHTML = rows.length
      ? rows.map((r) => `
          <div class="usage-bar-row">
            <span class="usage-bar-label" title="${r.provider}">${r.provider}</span>
            <span class="usage-bar-track">
              <span class="usage-bar-fill" style="width:${((r.cost || 0)/max*100).toFixed(1)}%"></span>
            </span>
            <span class="usage-bar-cost">${_fmtCost(r.cost || 0)}</span>
            <span class="usage-bar-calls">${r.calls || 0}×</span>
          </div>
        `).join("")
      : `<span class="ago">no calls today</span>`;
  }

  const purpEl = document.getElementById("usage-by-purpose");
  if (purpEl) {
    const rows = d.by_purpose_today || [];
    const max = Math.max(0.0001, ...rows.map((r) => r.cost || 0));
    purpEl.innerHTML = rows.length
      ? rows.map((r) => `
          <div class="usage-bar-row">
            <span class="usage-bar-label" title="${r.purpose}">${r.purpose}</span>
            <span class="usage-bar-track">
              <span class="usage-bar-fill" style="width:${((r.cost || 0)/max*100).toFixed(1)}%"></span>
            </span>
            <span class="usage-bar-cost">${_fmtCost(r.cost || 0)}</span>
            <span class="usage-bar-calls">${r.calls || 0}×</span>
          </div>
        `).join("")
      : `<span class="ago">no calls today</span>`;
  }

  // API keys block — masked fingerprints so the operator can confirm
  // which key is wired into each provider without leaking the secret.
  const keysEl = document.getElementById("usage-keys");
  if (keysEl) {
    const keys = d.keys || {};
    const providers = [
      { id: "gemini",    label: "Gemini" },
      { id: "anthropic", label: "Anthropic" },
    ];
    keysEl.innerHTML = providers.map((p) => {
      const k = keys[p.id] || {};
      if (!k.set) {
        return `
          <div class="usage-key-row inactive">
            <span class="usage-key-name">${p.label}</span>
            <span class="usage-key-env">—</span>
            <span class="usage-key-fp">not set</span>
          </div>`;
      }
      return `
        <div class="usage-key-row">
          <span class="usage-key-name">${p.label}</span>
          <span class="usage-key-env">${k.env_var || ""}</span>
          <span class="usage-key-fp" title="${p.label} key fingerprint">${k.fingerprint || "—"}</span>
        </div>`;
    }).join("");
  }

  const recentBody = document.querySelector("#usage-recent-table tbody");
  if (recentBody) {
    const rows = d.recent || [];
    recentBody.innerHTML = rows.length
      ? rows.map((r) => `
          <tr class="${r.success ? "" : "err"}">
            <td class="ago">${fmtAgo(r.ts)}</td>
            <td>${r.provider}</td>
            <td>${r.purpose}</td>
            <td class="num">${r.input_tokens || 0}</td>
            <td class="num">${r.output_tokens || 0}</td>
            <td class="num">${_fmtCost(r.cost_usd || 0)}</td>
          </tr>
        `).join("")
      : `<tr><td colspan="6" class="ago">no recent calls</td></tr>`;
  }
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
      ? `<span class="ingest-reasons-label">TOP REJECTION REASONS (hover for detail)</span>` +
        reasons.map((r) => {
          const hint = REJECTION_HINTS[r.reason] || r.reason;
          return `
            <span class="ingest-reason-pill" title="${hint}">
              <span class="ingest-reason-name">${r.reason || "—"}</span>
              <span class="ingest-reason-n">${r.n}</span>
            </span>
          `;
        }).join("")
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

  // Storage paths block — surfaces WHERE the data actually lives so
  // the operator can pop a shell open at the right path without
  // hunting through config.
  set("sp-db-path",  d.db_path     || "—");
  set("sp-data-dir", d.data_dir    || "—");
  set("sp-mount",    d.mountpoint  || "—");
  set("sp-fs",       d.fs_type && d.fs_type !== "n/a"
                       ? d.fs_type
                       : (d.platform || "—"));
  set("sp-host",     d.hostname    || "—");
  set("sp-pid",      d.pid != null ? String(d.pid) : "—");

  // Last prune summary line
  const pruneEl = document.getElementById("prune-summary");
  if (pruneEl) {
    const lp = d.last_prune;
    if (lp && lp.ran_at) {
      const removed = (lp.removed_articles || 0) + (lp.removed_reader || 0);
      pruneEl.textContent = `last ${fmtAgo(lp.ran_at)} · removed ${removed} rows`;
    } else {
      pruneEl.textContent = "never run yet — auto-runs daily";
    }
  }

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

// Manual prune button — fires the same prune the daily worker runs,
// then tick() refreshes the storage card so the user sees the result.
document.getElementById("prune-now")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const res = document.getElementById("prune-result");
  btn.disabled = true;
  btn.textContent = "PRUNING…";
  if (res) { res.textContent = ""; res.classList.remove("err", "ok"); }
  try {
    const r = await fetch("/api/storage/prune", { method: "POST" });
    const d = await r.json();
    if (res) {
      const removed = (d.removed_articles || 0) + (d.removed_reader || 0);
      const mb = (d.db_bytes_after || 0) / 1e6;
      res.textContent = `removed ${removed} rows · db now ${mb.toFixed(1)} MB`;
      res.classList.add("ok");
    }
    await tick();
  } catch (err) {
    if (res) { res.textContent = `failed: ${err.message}`; res.classList.add("err"); }
  }
  btn.disabled = false;
  btn.textContent = "PRUNE NOW";
});

// ── Action buttons (force refresh, rebuild, resummary, purge failed)
function setActionResult(text, kind = "ok") {
  const r = document.getElementById("action-result");
  if (!r) return;
  r.textContent = text;
  r.classList.remove("ok", "err", "busy");
  r.classList.add(kind);
}
async function runAction(label, url, opts) {
  setActionResult(`${label}…`, "busy");
  try {
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setActionResult(d.detail || `${label} failed (HTTP ${r.status})`, "err");
      return;
    }
    setActionResult(`${label} ok: ${JSON.stringify(d)}`, "ok");
    await tick();
  } catch (err) {
    setActionResult(`${label} failed: ${err.message}`, "err");
  }
}
document.getElementById("act-self-heal")?.addEventListener("click", () =>
  runAction("SELF-HEAL NOW", "/api/admin/self-heal", { method: "POST" })
);
document.getElementById("act-run-cycle")?.addEventListener("click", () =>
  runAction("RUN FULL CYCLE", "/api/admin/run-cycle?validate=40", { method: "POST" })
);
document.getElementById("act-refresh")?.addEventListener("click", () =>
  runAction("FORCE REFRESH", "/api/refresh", { method: "POST" })
);
document.getElementById("act-rebuild")?.addEventListener("click", () => {
  if (!confirm("Reset every article to unvalidated and re-run the validator? Safe — does not delete anything.")) return;
  runAction("REBUILD VALIDATIONS", "/api/admin/rebuild?purge_now=0", { method: "POST" });
});
document.getElementById("act-resummary")?.addEventListener("click", () =>
  runAction("RE-SUMMARIZE 500", "/api/ingest/resummary?limit=500", { method: "POST" })
);
document.getElementById("act-strict")?.addEventListener("click", () => {
  if (!confirm("Re-validate the entire archive against the strictest current rules, then permanently DELETE everything that fails? This can take a few minutes.")) return;
  runAction("STRICT REVALIDATE", "/api/admin/strict-revalidate?batch_size=30", { method: "POST" });
});

// Archive actions — move (not delete) older articles out of the
// active feed. Reversible via UNARCHIVE ALL.
document.getElementById("act-archive-7")?.addEventListener("click", () => {
  if (!confirm("Move every article older than 7 days into the archive pool? Reversible.")) return;
  runAction("ARCHIVE 7d", "/api/admin/archive-old?days=7", { method: "POST" });
});
document.getElementById("act-archive-3")?.addEventListener("click", () => {
  if (!confirm("Move every article older than 3 days into the archive pool? Reversible.")) return;
  runAction("ARCHIVE 3d", "/api/admin/archive-old?days=3", { method: "POST" });
});
document.getElementById("act-unarchive")?.addEventListener("click", () => {
  if (!confirm("Restore every archived article back into the active pool?")) return;
  runAction("UNARCHIVE ALL", "/api/admin/unarchive-all", { method: "POST" });
});

// Factory reset: nuke articles + reader_results + blocked_urls. Keeps
// user accounts. Use when the DB has grown unwieldy.
document.getElementById("act-factory-reset")?.addEventListener("click", () => {
  const msg = "PERMANENTLY WIPE every article + cached body + blocked URL?\n\n" +
              "User accounts are kept. The next refresh cycle will reseed\n" +
              "the feed from live RSS. THIS CANNOT BE UNDONE.";
  if (!confirm(msg)) return;
  if (!confirm("Are you absolutely sure? Type-confirm by clicking OK one more time.")) return;
  runAction("FACTORY RESET", "/api/admin/factory-reset", { method: "POST" });
});

// Manual validation — process 20 pending articles inline. Replaces the
// disabled background validator. Click repeatedly to chip through.
document.getElementById("act-validate-batch")?.addEventListener("click", () => {
  runAction("RUN VALIDATION (20)", "/api/admin/run-validation?batch_size=20", { method: "POST" });
});

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

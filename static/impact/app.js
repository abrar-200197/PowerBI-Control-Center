/**
 * PBI Impact Explorer — client app
 *
 * Architecture: browser NEVER downloads SharePoint or large catalog files.
 * Server holds SP source-of-truth + disk mirror; UI calls thin APIs only:
 *   GET /api/catalog/impact/tables         — flat rows for grid
 *   GET /api/catalog/impact/table          — one table drawer detail
 *   GET /api/catalog/impact/lookup         — name search with blast radius
 *   GET /api/catalog/impact/model-details  — one semantic model popup
 *   GET /api/catalog/data/summary.json     — small stats
 */
const state = {
  rows: [],
  filtered: [],
  summary: null,
  catalog: null, // not loaded in browser (server-only workspace_catalog)
  sortKey: "reportCount",
  sortDir: "desc",
  page: 1,
  pageSize: 50,
  selectedKey: null,
  drawerReports: [],
  selectedWorkspaceId: null,
  wsReportFilter: "",
  _reportTables: [],
  _modelDetails: null,
  _modelTab: "focus",
};

const $ = (sel) => document.querySelector(sel);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
const cssEsc = (s) => {
  try {
    if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(s));
  } catch (_) { /* ignore */ }
  return String(s).replace(/["\\]/g, "\\$&");
};

function sourceClass(t) {
  const x = (t || "").toLowerCase();
  if (x === "sql" || x === "sqlnative") return "sql";
  if (x === "modeltable") return "model";
  if (x.includes("analysis")) return "as";
  if (x.includes("odata")) return "odata";
  if (x.includes("snow")) return "snow";
  return "";
}

function isPhysical(row) {
  return row.sourceType && row.sourceType !== "ModelTable";
}

/** Human label for a physical/source object (EDW table, SSAS, etc.) */
function sourceObjectName(s) {
  if (!s) return "";
  const schema = s.schema || "";
  const table = s.table || s.object_name || s.objectName || "";
  if (schema && table) return `${schema}.${table}`;
  return table || "";
}

function sourceLocation(s) {
  if (!s) return "";
  const parts = [];
  if (s.server) parts.push(s.server);
  if (s.database) parts.push(s.database);
  return parts.join(" / ");
}

function isPhysicalSource(s) {
  const t = (s?.source_type || s?.sourceType || "").toLowerCase();
  return t && t !== "modeltable" && t !== "unknown";
}

/** Best source line(s) for a model table — for UI under the Power BI name */
function modelTableSourceSummary(t) {
  const sources = t?.sources || [];
  const physical = sources.filter(isPhysicalSource);
  const list = physical.length ? physical : sources;
  if (!list.length) {
    return { status: "unknown", lines: ["Source not resolved from scan"] };
  }
  let fullyMapped = false;
  const lines = list.map((s) => {
    const type = s.source_type || s.sourceType || "Source";
    const obj = sourceObjectName(s);
    const loc = sourceLocation(s);
    if (isPhysicalSource(s)) {
      if (obj) {
        fullyMapped = true;
        return [obj, loc, type].filter(Boolean).join(" · ");
      }
      // Server/db known from Sql.Database but FROM clause not found (proc/TVF/complex SQL)
      return [
        "Table/object not found in SQL text (custom query)",
        loc || null,
        type,
      ].filter(Boolean).join(" · ");
    }
    return `Power BI name only — source SQL not in expression · ${obj || t.name || ""}`;
  });
  return {
    status: fullyMapped ? "mapped" : physical.length ? "partial" : "unmapped",
    lines: [...new Set(lines)],
  };
}

function normalizeTables(payload) {
  const tables = payload.tables || {};
  return Object.values(tables).map((t) => {
    const s = t.impactSummary || {};
    return {
      tableKey: t.tableKey,
      table: t.table || "—",
      sourceType: t.sourceType || "Unknown",
      server: t.server || "",
      database: t.database || "",
      schema: t.schema || "",
      modelTableNames: t.modelTableNames || [],
      datasets: t.datasets || [],
      reportCount: s.reportCount || 0,
      datasetCount: s.datasetCount || 0,
      workspaceCount: s.workspaceCount || 0,
      searchText: [
        t.table, t.tableKey, t.server, t.database, t.schema, t.sourceType,
        ...(t.modelTableNames || []),
      ].join(" ").toLowerCase(),
    };
  });
}

function setLoadProgress(msg) {
  const el = $("#loadState");
  if (!el) return;
  el.innerHTML = `<div class="spinner"></div><div><strong>${msg}</strong>
    <div class="muted small">Server reads SharePoint; browser only gets thin API JSON</div></div>`;
}

async function fetchJsonNoCache(url, { refresh = false, timeoutMs = 300000, allowHttpCache = false } = {}) {
  const sep = url.includes("?") ? "&" : "?";
  // Only bust cache when forcing refresh — otherwise reuse browser private cache (2 min on server)
  let full = refresh || !allowHttpCache ? `${url}${sep}_=${Date.now()}` : url;
  if (refresh) full += `${full.includes("?") ? "&" : "?"}refresh=1`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(full, {
      credentials: "same-origin",
      cache: allowHttpCache && !refresh ? "default" : "no-store",
      headers: allowHttpCache && !refresh
        ? { Accept: "application/json" }
        : { "Cache-Control": "no-cache", Pragma: "no-cache", Accept: "application/json" },
      signal: ctrl.signal,
    });
    return res;
  } finally {
    clearTimeout(timer);
  }
}

const IMPACT_CACHE_KEY = "pbi_cc_impact_tables_v2";
const IMPACT_CACHE_TTL_MS = 10 * 60 * 1000; // 10 min session reuse

function readImpactSessionCache() {
  try {
    const raw = sessionStorage.getItem(IMPACT_CACHE_KEY);
    if (!raw) return null;
    const pack = JSON.parse(raw);
    if (!pack || !pack.ts || !Array.isArray(pack.rows)) return null;
    if (Date.now() - pack.ts > IMPACT_CACHE_TTL_MS) return null;
    return pack;
  } catch (_) {
    return null;
  }
}

function writeImpactSessionCache(payload) {
  try {
    sessionStorage.setItem(
      IMPACT_CACHE_KEY,
      JSON.stringify({
        ts: Date.now(),
        generatedAt: payload.generatedAt || null,
        stats: payload.stats || {},
        rows: payload.rows || [],
      })
    );
  } catch (_) {
    /* quota — ignore */
  }
}

/** Expand compact server row (v2) or legacy full row into UI row shape. */
function expandImpactRow(r) {
  const isCompact = r && (r.k != null || r.t != null) && r.tableKey == null && r.table == null;
  if (isCompact || (r && r.k && r.rc != null)) {
    const table = r.t || "—";
    const tableKey = r.k || "";
    const modelTableNames = r.mn || [];
    const sourceType = r.st || "Unknown";
    const server = r.sv || "";
    const database = r.db || "";
    const schema = r.sc || "";
    return {
      tableKey,
      table,
      sourceType,
      server,
      database,
      schema,
      modelTableNames,
      datasets: [],
      reportCount: r.rc || 0,
      datasetCount: r.dc || 0,
      workspaceCount: r.wc || 0,
      searchText: [table, tableKey, server, database, schema, sourceType, ...modelTableNames]
        .join(" ")
        .toLowerCase(),
    };
  }
  // Legacy / full shape
  return {
    tableKey: r.tableKey,
    table: r.table || "—",
    sourceType: r.sourceType || "Unknown",
    server: r.server || "",
    database: r.database || "",
    schema: r.schema || "",
    modelTableNames: r.modelTableNames || [],
    datasets: [],
    reportCount: r.reportCount || 0,
    datasetCount: r.datasetCount || 0,
    workspaceCount: r.workspaceCount || 0,
    searchText: r.searchText || [
      r.table, r.tableKey, r.server, r.database, r.schema, r.sourceType,
      ...(r.modelTableNames || []),
    ].join(" ").toLowerCase(),
  };
}

async function loadJsonFile(path, label, opts = {}) {
  setLoadProgress(`Loading ${label}…`);
  const res = await fetchJsonNoCache(path, opts);
  if (!res.ok) {
    let detail = `${path} HTTP ${res.status}`;
    try {
      const errBody = await res.json();
      if (errBody.detail) detail += ` — ${errBody.detail}`;
      if (errBody.expectedPath) detail += ` (path: ${errBody.expectedPath})`;
    } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  setLoadProgress(`Parsing ${label}…`);
  const data = await res.json();
  return { data, source: res.headers.get("X-Data-Source") || "sharepoint" };
}

async function loadData(forceRefresh = false) {
  const reloadBtn = $("#reloadBtn");
  if (reloadBtn && !reloadBtn.classList.contains("hidden")) {
    reloadBtn.disabled = true;
    reloadBtn.textContent = forceRefresh ? "Refreshing…" : "Loading…";
  }
  $("#loadState")?.classList.remove("hidden");
  $("#errorState")?.classList.add("hidden");
  $("#appContent")?.classList.add("hidden");
  const opts = { refresh: forceRefresh };

  try {
    try {
      setLoadProgress("Checking server…");
      await fetchJsonNoCache("/api/catalog/status", { timeoutMs: 30000 });
    } catch (_) { /* optional */ }

    // 1) Prefer session cache (instant return visits within 10 min)
    let tpack = null;
    let fromSession = false;
    if (forceRefresh) {
      try { sessionStorage.removeItem(IMPACT_CACHE_KEY); } catch (_) { /* ignore */ }
    } else {
      const cached = readImpactSessionCache();
      if (cached) {
        tpack = { success: true, rows: cached.rows, generatedAt: cached.generatedAt, stats: cached.stats, source: "session" };
        fromSession = true;
        setLoadProgress("Restoring impact tables from session…");
      }
    }

    // 2) Small summary (optional KPIs) — skip when session already has stats
    let summary = state.summary;
    if (!fromSession || !tpack?.stats || !Object.keys(tpack.stats || {}).length) {
      try {
        const s = await loadJsonFile("/api/catalog/data/summary.json", "summary (~KB)", opts);
        summary = s.data;
        state.summary = summary;
      } catch (e) {
        console.warn("summary load failed", e);
      }
    }

    // 3) Thin impact table list from server if no session hit
    if (!tpack) {
      setLoadProgress("Loading impact tables…");
      const tablesUrl = "/api/catalog/impact/tables";
      const tres = await fetchJsonNoCache(tablesUrl, {
        refresh: forceRefresh,
        timeoutMs: 180000,
        allowHttpCache: !forceRefresh,
      });
      if (!tres.ok) {
        const errBody = await tres.json().catch(() => ({}));
        throw new Error(errBody.error || `impact/tables HTTP ${tres.status}`);
      }
      tpack = await tres.json();
      if (!tpack.success) throw new Error(tpack.error || "impact/tables failed");
      writeImpactSessionCache(tpack);
    }

    state.rows = (tpack.rows || []).map(expandImpactRow);

    const gen = tpack.generatedAt || summary?.generatedAt || "";
    const stats = tpack.stats || summary?.stats || {};
    const runMeta = $("#runMeta");
    if (runMeta && !runMeta.classList.contains("hidden")) {
      const srcLabel = fromSession ? "Session cache" : (tpack.source === "server-thin" ? "Server (cached)" : "Server");
      runMeta.innerHTML =
        `Source: <strong>${srcLabel}</strong> · ` +
        `${fmt(stats.workspaceCount)} ws · ${fmt(stats.reportCount)} reports · ` +
        `${fmt(state.rows.length)} tables` +
        (gen ? ` · ${new Date(gen).toLocaleString()}` : "");
    }

    populateSourceFilter();
    applyFilters();
    try { renderDashboard(); } catch (_) { /* optional */ }
    try { renderInsights(); } catch (_) { /* optional */ }
    try { renderCoverageBanner(); } catch (_) { /* optional */ }

    $("#loadState")?.classList.add("hidden");
    $("#appContent")?.classList.remove("hidden");
    setView("tables");
    if (reloadBtn && !reloadBtn.classList.contains("hidden")) {
      reloadBtn.disabled = false;
      reloadBtn.textContent = "Reload";
    }

    // Workspace browser removed from thin path — use Report Catalog for estate browse.
    // Keep optional list empty rather than downloading workspace_catalog.
    state.catalog = null;
    const wsList = $("#wsList");
    if (wsList) {
      wsList.innerHTML = `<div class="muted small">Workspace estate browse uses <strong>Report Catalog</strong>. Impact Explorer stays on table blast-radius (thin API).</div>`;
    }
  } catch (err) {
    $("#loadState").classList.add("hidden");
    $("#errorState").classList.remove("hidden");
    const aborted = err?.name === "AbortError";
    $("#errorState").innerHTML = `<div><strong>Failed to load impact data</strong>
      <div class="small" style="margin-top:8px">${escapeHtml(aborted ? "Request timed out. Retry Reload." : err.message)}</div>
      <div class="small muted" style="margin-top:10px">
        Server loads SharePoint into a disk cache; browser only calls thin APIs.<br/>
        Ensure extract published latest/ and restart <code>python app.py</code>.
      </div></div>`;
    if (reloadBtn) {
      reloadBtn.disabled = false;
      reloadBtn.textContent = "Reload";
    }
  }
}

function populateSourceFilter() {
  const set = new Set(state.rows.map((r) => r.sourceType).filter(Boolean));
  const sel = $("#sourceFilter");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML =
    `<option value="">Source: All</option>` +
    [...set].sort().map((s) => `<option value="${escapeAttr(s)}">${escapeHtml(s)}</option>`).join("");
  sel.value = cur;
}

function applyFilters() {
  const q = ($("#searchInput")?.value || "").trim().toLowerCase();
  const src = $("#sourceFilter")?.value || "";
  const minR = Number($("#minReports")?.value || 0);
  const res = $("#resolutionFilter")?.value || "";

  state.filtered = state.rows.filter((r) => {
    if (q && !r.searchText.includes(q)) return false;
    if (src && r.sourceType !== src) return false;
    if (r.reportCount < minR) return false;
    if (res === "physical" && !isPhysical(r)) return false;
    if (res === "model" && isPhysical(r)) return false;
    return true;
  });

  sortFiltered();
  state.page = 1;
  renderTable();
}

function sortFiltered() {
  const k = state.sortKey;
  const dir = state.sortDir === "asc" ? 1 : -1;
  state.filtered.sort((a, b) => {
    const av = a[k], bv = b[k];
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
    return String(av || "").localeCompare(String(bv || ""), undefined, { sensitivity: "base" }) * dir;
  });
}

function renderTable() {
  const total = state.filtered.length;
  const pages = Math.max(1, Math.ceil(total / state.pageSize));
  if (state.page > pages) state.page = pages;
  const start = (state.page - 1) * state.pageSize;
  const slice = state.filtered.slice(start, start + state.pageSize);
  $("#resultCount").textContent = `${fmt(total)} tables · page ${state.page}/${pages}`;
  $("#pageInfo").textContent = `${fmt(start + 1)}–${fmt(Math.min(start + state.pageSize, total))} of ${fmt(total)}`;
  $("#prevPage").disabled = state.page <= 1;
  $("#nextPage").disabled = state.page >= pages;

  const tb = $("#impactTable tbody");
  tb.innerHTML = slice.map((r) => {
    const aliases = (r.modelTableNames || []).filter((n) => n && n.toLowerCase() !== (r.table || "").toLowerCase());
    const aliasLine = aliases.length
      ? `In Power BI also as: ${aliases.slice(0, 3).join(", ")}${aliases.length > 3 ? "…" : ""}`
      : (isPhysical(r) ? "Mapped from EDW/source" : "Power BI name only");
    return `
    <tr>
      <td><button class="linkish" data-open="${escapeAttr(r.tableKey)}">${escapeHtml(r.table)}</button>
        <div class="muted small">${escapeHtml(aliasLine)}</div></td>
      <td><span class="pill ${sourceClass(r.sourceType)}">${escapeHtml(isPhysical(r) ? (r.sourceType || "Source") : "PBI only")}</span></td>
      <td class="mono small">${escapeHtml(r.server || "—")}</td>
      <td class="mono small">${escapeHtml(r.database || "—")}</td>
      <td class="num"><strong>${fmt(r.reportCount)}</strong></td>
      <td class="num">${fmt(r.datasetCount)}</td>
      <td class="num">${fmt(r.workspaceCount)}</td>
      <td><button class="btn ghost sm" data-open="${escapeAttr(r.tableKey)}">Details</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No rows match filters.</td></tr>`;

  tb.querySelectorAll("[data-open]").forEach((btn) => {
    btn.addEventListener("click", () => openDrawer(btn.getAttribute("data-open")));
  });
}

function renderDashboard() {
  // Dashboard view removed from UI (use outer Home / catalog for estate KPIs)
  if (!$("#kpiGrid") || $("#kpiGrid").classList.contains("hidden")) return;
  const rows = state.rows;
  const physical = rows.filter(isPhysical);
  const modelOnly = rows.length - physical.length;
  const totalReports = state.summary?.stats?.reportCount;
  const high = rows.filter((r) => r.reportCount >= 50).length;

  $("#kpiGrid").innerHTML = [
    kpi("Table keys", rows.length, "Unique entries in impact index"),
    kpi("Physical sources", physical.length, `${modelOnly.toLocaleString()} model-only names`),
    kpi("Reports scanned", totalReports ?? "—", `${state.summary?.stats?.datasetCount ?? "—"} datasets`),
    kpi("High impact (≥50 reports)", high, "Prioritize these for migration"),
  ].join("");

  const top = [...rows].sort((a, b) => b.reportCount - a.reportCount).slice(0, 15);
  const maxR = top[0]?.reportCount || 1;
  $("#topTablesChart").innerHTML = top.map((r) => bar(r.table, r.reportCount, maxR, () => r.tableKey)).join("");
  bindBars("#topTablesChart", top);

  const mix = countBy(rows, (r) => r.sourceType || "Unknown");
  const mixArr = Object.entries(mix).sort((a, b) => b[1] - a[1]);
  const maxM = mixArr[0]?.[1] || 1;
  $("#sourceMix").innerHTML = mixArr.map(([k, v]) => bar(k, v, maxM)).join("");
  $("#sourceCallout").textContent =
    `${pct(modelOnly, rows.length)} of keys are model-only (no SQL server parsed). ` +
    `Filter to "Physical source" when planning EDW table cutover. Sql keys: ${mix.Sql || 0}.`;

  const servers = {};
  for (const r of physical) {
    const key = `${r.server || "?"}||${r.database || "?"}`;
    if (!servers[key]) servers[key] = { server: r.server || "—", database: r.database || "—", tables: 0, reports: 0 };
    servers[key].tables += 1;
    servers[key].reports += r.reportCount;
  }
  const srows = Object.values(servers).sort((a, b) => b.reports - a.reports).slice(0, 20);
  $("#serverTable").innerHTML = `
    <table><thead><tr><th>Server</th><th>Database</th><th class="num">Table keys</th><th class="num">Σ report refs</th></tr></thead>
    <tbody>${srows.map((s) => `<tr><td class="mono small">${escapeHtml(s.server)}</td><td class="mono small">${escapeHtml(s.database)}</td>
      <td class="num">${fmt(s.tables)}</td><td class="num">${fmt(s.reports)}</td></tr>`).join("") || "<tr><td colspan='4'>No physical sources</td></tr>"}</tbody></table>`;
}

function renderInsights() {
  if (!$("#insightsGrid") || $("#insightsGrid").classList.contains("hidden")) return;
  const rows = state.rows;
  const physicalHigh = rows.filter((r) => isPhysical(r) && r.reportCount >= 10)
    .sort((a, b) => b.reportCount - a.reportCount).slice(0, 20);
  const shared = rows.filter((r) => r.datasetCount >= 5 && r.reportCount >= 10)
    .sort((a, b) => b.datasetCount - a.datasetCount).slice(0, 20);
  const edw = rows.filter((r) => /edw|ashley-edw|ashley_edw/i.test(r.searchText))
    .sort((a, b) => b.reportCount - a.reportCount).slice(0, 25);
  const unresolvedBig = rows.filter((r) => !isPhysical(r) && r.reportCount >= 30)
    .sort((a, b) => b.reportCount - a.reportCount).slice(0, 20);

  $("#insightsGrid").innerHTML = [
    insightCard("Critical EDW / SQL cutovers", "Physical sources with the largest report blast radius. Validate these first before Fabric remap.", physicalHigh, "reports"),
    insightCard("Wide reuse (many datasets)", "Same logical table name appears across many semantic models — one change may need multi-team coord.", shared, "datasets"),
    insightCard("Ashley EDW touchpoints", "Keys matching EDW host/database naming from your estate.", edw, "reports"),
    insightCard("Needs lineage enrichment", "High-impact model-only names (expression didn’t resolve server/table). Improve M parse or datasource details.", unresolvedBig, "reports"),
  ].join("");

  $("#insightsGrid").querySelectorAll("[data-open]").forEach((el) => {
    el.addEventListener("click", () => openDrawer(el.getAttribute("data-open")));
  });
}

function insightCard(title, desc, list, mode) {
  return `<div class="card insight-card">
    <div class="tag">Action</div>
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(desc)}</p>
    <div class="insight-list">${list.map((r) => `
      <div class="insight-row" data-open="${escapeAttr(r.tableKey)}">
        <div>
          <strong>${escapeHtml(r.table)}</strong>
          <div class="muted small">${escapeHtml(r.server || r.sourceType)}${r.database ? " · " + escapeHtml(r.database) : ""}</div>
        </div>
        <div class="num mono small">${mode === "datasets" ? fmt(r.datasetCount) + " ds" : fmt(r.reportCount) + " rpts"}</div>
      </div>`).join("") || `<div class="muted small">No items</div>`}</div>
  </div>`;
}

async function openDrawer(tableKey) {
  const row = state.rows.find((r) => r.tableKey === tableKey);
  if (!row) return;
  state.selectedKey = tableKey;
  const physical = isPhysical(row);
  const sourceLabel = [
    row.schema && row.table ? `${row.schema}.${row.table}` : row.table,
    row.server,
    row.database,
  ].filter(Boolean).join(" · ");

  $("#drawerTitle").textContent = physical
    ? (row.schema && row.table ? `${row.schema}.${row.table}` : row.table)
    : row.table;
  $("#drawerSub").innerHTML = physical
    ? `Source in EDW/DB: <strong>${escapeHtml(sourceLabel)}</strong><br/>Also appears in Power BI under name(s): <strong>${escapeHtml((row.modelTableNames || []).slice(0, 8).join(", ") || row.table)}</strong>`
    : `Power BI table name (source SQL not fully mapped)<br/><span class="mono">${escapeHtml(row.tableKey)}</span>`;

  $("#drawerKpis").innerHTML = [
    kpi("Reports affected", row.reportCount),
    kpi("Datasets affected", row.datasetCount),
    kpi("Workspaces", row.workspaceCount),
  ].join("");
  $("#drawerReports").innerHTML = `<div class="muted small">Loading affected reports…</div>`;
  $("#drawerDatasets").innerHTML = `<div class="muted small">Loading datasets…</div>`;
  const aliases = row.modelTableNames || [];
  $("#drawerModels").innerHTML = aliases.length
    ? aliases.slice(0, 100).map((n) => `<span class="chip" title="Name used inside a Power BI model">${escapeHtml(n)}</span>`).join("")
    : `<span class="muted small">No alternate Power BI names recorded</span>`;

  $("#drawer").classList.remove("hidden");
  $("#reportDrawer").classList.add("hidden");
  $("#drawerBackdrop").classList.remove("hidden");
  $("#drawer").setAttribute("aria-hidden", "false");

  // Detail blast radius — one object from server (never full index in browser)
  let datasets = row.datasets || [];
  try {
    const res = await fetchJsonNoCache(
      `/api/catalog/impact/table?key=${encodeURIComponent(tableKey)}`,
      { timeoutMs: 120000 }
    );
    if (res.ok) {
      const body = await res.json();
      if (body.success && body.table) {
        datasets = body.table.datasets || [];
        row.datasets = datasets;
        const s = body.table.impactSummary || {};
        if (s.reportCount != null) row.reportCount = s.reportCount;
        if (s.datasetCount != null) row.datasetCount = s.datasetCount;
        if (s.workspaceCount != null) row.workspaceCount = s.workspaceCount;
        if (body.table.modelTableNames) row.modelTableNames = body.table.modelTableNames;
        $("#drawerKpis").innerHTML = [
          kpi("Reports affected", row.reportCount),
          kpi("Datasets affected", row.datasetCount),
          kpi("Workspaces", row.workspaceCount),
        ].join("");
        const al = row.modelTableNames || [];
        $("#drawerModels").innerHTML = al.length
          ? al.slice(0, 100).map((n) => `<span class="chip">${escapeHtml(n)}</span>`).join("")
          : `<span class="muted small">No alternate Power BI names recorded</span>`;
      }
    }
  } catch (e) {
    console.warn("impact table detail failed", e);
  }

  const map = new Map();
  for (const ds of datasets) {
    for (const rep of ds.reports || []) {
      const id = rep.reportId || rep.reportName;
      if (!map.has(id)) {
        map.set(id, {
          ...rep,
          datasetId: ds.datasetId || rep.datasetId || "",
          datasetName: ds.datasetName || "",
          workspaceId: rep.workspaceId || ds.workspaceId || "",
          workspaceName: rep.workspaceName || ds.workspaceName || "",
          datasetWorkspace: ds.workspaceName,
          modelTableName: ds.modelTableName || "",
        });
      }
    }
  }
  state.drawerReports = [...map.values()].sort((a, b) =>
    String(a.reportName || "").localeCompare(String(b.reportName || ""))
  );
  if ($("#drawerReportFilter")) $("#drawerReportFilter").value = "";
  renderDrawerReports();

  $("#drawerDatasets").innerHTML = datasets
    .slice()
    .sort((a, b) => (b.reports?.length || 0) - (a.reports?.length || 0))
    .map((d) => `<div class="list-item list-item-clickable" data-ds-open="1"
        data-dataset-id="${escapeAttr(d.datasetId || "")}"
        data-workspace-id="${escapeAttr(d.workspaceId || "")}"
        data-dataset-name="${escapeAttr(d.datasetName || "")}"
        data-workspace-name="${escapeAttr(d.workspaceName || "")}"
        data-model-table="${escapeAttr(d.modelTableName || "")}">
      <div class="list-item-main">
        <button type="button" class="linkish ds-open">${escapeHtml(d.datasetName || d.datasetId)}</button>
        <div class="sub">${escapeHtml(d.workspaceName || "")}<br/>
          Power BI table name in this model: <strong>${escapeHtml(d.modelTableName || "—")}</strong>
          ${physical ? `<br/>Source object: <strong>${escapeHtml(row.table || "—")}</strong>` : ""}
          · ${(d.reports || []).length} reports</div>
      </div>
      <button type="button" class="btn ghost sm ds-open">View model</button>
    </div>`)
    .join("") || `<div class="muted">No datasets</div>`;

  $("#drawerDatasets").querySelectorAll("[data-ds-open]").forEach((root) => {
    const open = (ev) => {
      if (ev) ev.stopPropagation();
      openModelModal({
        datasetId: root.getAttribute("data-dataset-id"),
        workspaceId: root.getAttribute("data-workspace-id"),
        datasetName: root.getAttribute("data-dataset-name"),
        workspaceName: root.getAttribute("data-workspace-name"),
        modelTableName: root.getAttribute("data-model-table"),
        reportName: "",
        reportId: "",
      });
    };
    root.addEventListener("click", (ev) => {
      // Single open path for row or inner button
      open(ev);
    });
  });
}

function renderDrawerReports() {
  const q = ($("#drawerReportFilter").value || "").toLowerCase();
  const list = state.drawerReports.filter((r) =>
    !q || `${r.reportName} ${r.workspaceName} ${r.datasetName}`.toLowerCase().includes(q)
  );
  $("#drawerReports").innerHTML = list.map((r, i) => {
    const rid = r.reportId || "";
    const rname = r.reportName || r.reportId || "—";
    const ws = r.workspaceName || "";
    const ds = r.datasetName || "";
    const dsId = r.datasetId || "";
    const wsId = r.workspaceId || "";
    const modelTable = r.modelTableName || "";
    return `
    <div class="list-item list-item-clickable" data-report-idx="${i}" title="View semantic model details">
      <div class="list-item-main">
        <button type="button" class="linkish report-open" data-report-idx="${i}">${escapeHtml(rname)}</button>
        <div class="sub">
          ${escapeHtml(ws)}${ws && ds ? " · " : ""}${ds ? `Model: <button type="button" class="linkish inline-link model-open" data-report-idx="${i}">${escapeHtml(ds)}</button>` : ""}
          ${modelTable ? `<span class="muted"> · table ${escapeHtml(modelTable)}</span>` : ""}
        </div>
      </div>
      <button type="button" class="btn ghost sm model-open" data-report-idx="${i}"
        data-dataset-id="${escapeAttr(dsId)}" data-workspace-id="${escapeAttr(wsId)}"
        data-report-id="${escapeAttr(rid)}" data-report-name="${escapeAttr(rname)}"
        data-model-table="${escapeAttr(modelTable)}">View model</button>
    </div>`;
  }).join("") || `<div class="muted small">No reports</div>`;

  $("#drawerReports").querySelectorAll(".report-open, .model-open, .list-item-clickable").forEach((el) => {
    el.addEventListener("click", (ev) => {
      // Avoid double-fire when clicking inner button inside the row
      if (el.classList.contains("list-item-clickable") && ev.target.closest("button")) return;
      const idx = Number(el.getAttribute("data-report-idx"));
      const rep = state.drawerReports[idx];
      if (!rep) return;
      openModelModal({
        datasetId: rep.datasetId,
        workspaceId: rep.workspaceId,
        reportId: rep.reportId,
        reportName: rep.reportName,
        modelTableName: rep.modelTableName,
        datasetName: rep.datasetName,
        workspaceName: rep.workspaceName,
      });
    });
  });
}

function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer").setAttribute("aria-hidden", "true");
  if ($("#reportDrawer").classList.contains("hidden") && (!$("#modelModal") || $("#modelModal").classList.contains("hidden"))) {
    $("#drawerBackdrop").classList.add("hidden");
  }
}

function closeModelModal() {
  const modal = $("#modelModal");
  if (!modal) return;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  state._modelDetails = null;
  // Keep table impact drawer; only hide backdrop if nothing open
  if ($("#drawer").classList.contains("hidden") && $("#reportDrawer").classList.contains("hidden")) {
    $("#drawerBackdrop").classList.add("hidden");
  }
}

async function openModelModal(ctx) {
  const modal = $("#modelModal");
  if (!modal) return;
  if (!ctx.datasetId) {
    alert("No semantic model id on this report — cannot load details.");
    return;
  }

  const focusRow = state.rows.find((r) => r.tableKey === state.selectedKey) || {};
  const focusTable = focusRow.table || "";
  const focusAliases = focusRow.modelTableNames || [];

  $("#modelModalTitle").textContent = ctx.datasetName || "Semantic model";
  $("#modelModalSub").innerHTML = [
    ctx.workspaceName ? escapeHtml(ctx.workspaceName) : "",
    ctx.reportName ? `Report: <strong>${escapeHtml(ctx.reportName)}</strong>` : "",
    focusTable || ctx.modelTableName
      ? `Focused on <strong>${escapeHtml(ctx.modelTableName || focusTable)}</strong>`
      : "",
  ].filter(Boolean).join(" · ");

  state._modelTab = "focus";
  state._modelDetails = null;
  $("#modelModalTabs").innerHTML = "";
  $("#modelModalBody").innerHTML = `<div class="muted small" style="padding:12px 0">Loading model details from catalog…</div>`;
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  // Keep drawer backdrop so impact drawer stays dimmed underneath
  $("#drawerBackdrop").classList.remove("hidden");

  const params = new URLSearchParams({
    dataset_id: ctx.datasetId || "",
    workspace_id: ctx.workspaceId || "",
    focus_table: focusTable || ctx.modelTableName || "",
    model_table: ctx.modelTableName || "",
    report_name: ctx.reportName || "",
    report_id: ctx.reportId || "",
  });

  try {
    const res = await fetchJsonNoCache(
      `/api/catalog/impact/model-details?${params.toString()}`,
      { timeoutMs: 120000 }
    );
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.success) {
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    // If server focus miss but we have aliases, mark matching tables client-side
    if (!(body.focusTables || []).length && (focusAliases.length || focusTable || ctx.modelTableName)) {
      const needles = new Set(
        [focusTable, ctx.modelTableName, ...focusAliases].filter(Boolean).map((s) => String(s).toLowerCase())
      );
      body.focusTables = (body.tables || []).filter((t) => needles.has(String(t.name || "").toLowerCase()));
      body.focusTables.forEach((t) => { t.isFocus = true; });
    }
    state._modelDetails = body;
    if ($("#modelModalTitle") && body.datasetName) {
      $("#modelModalTitle").textContent = body.datasetName;
    }
    if ($("#modelModalSub")) {
      const ws = body.workspaceName || ctx.workspaceName || "";
      const focusLabel = (body.focusTables && body.focusTables[0]?.name)
        || ctx.modelTableName
        || focusTable
        || body.focusTable
        || "";
      $("#modelModalSub").innerHTML = [
        ws ? escapeHtml(ws) : "",
        ctx.reportName ? `Report: <strong>${escapeHtml(ctx.reportName)}</strong>` : "",
        focusLabel ? `Focused on <strong>${escapeHtml(focusLabel)}</strong>` : "",
        body.tableCount != null ? `${fmt(body.tableCount)} tables` : "",
      ].filter(Boolean).join(" · ");
    }
    // Default tab: focused table if any, else all tables
    state._modelTab = (body.focusTables || []).length ? "focus" : "all";
    renderModelModal();
  } catch (e) {
    console.warn("model-details failed", e);
    $("#modelModalBody").innerHTML = `<div class="callout" style="border-color:#FCA5A5;color:#991B1B">
      Could not load model details: ${escapeHtml(e.message || String(e))}
    </div>`;
  }
}

function renderModelModal() {
  const body = state._modelDetails;
  if (!body) return;
  const focusTables = body.focusTables || [];
  const allTables = body.tables || [];
  const measures = body.measures || [];
  const hasFocus = focusTables.length > 0;

  const tabs = [
    hasFocus ? { id: "focus", label: `This table (${focusTables.length})` } : null,
    { id: "all", label: `All tables (${allTables.length})` },
    { id: "measures", label: `Measures (${measures.length})` },
  ].filter(Boolean);

  if (!tabs.find((t) => t.id === state._modelTab)) {
    state._modelTab = tabs[0]?.id || "all";
  }

  $("#modelModalTabs").innerHTML = tabs.map((t) => `
    <button type="button" class="model-tab ${state._modelTab === t.id ? "active" : ""}" data-tab="${t.id}" role="tab" aria-selected="${state._modelTab === t.id}">
      ${escapeHtml(t.label)}
    </button>`).join("");

  $("#modelModalTabs").querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state._modelTab = btn.getAttribute("data-tab");
      renderModelModal();
    });
  });

  const pane = $("#modelModalBody");
  if (state._modelTab === "measures") {
    pane.innerHTML = renderMeasuresPane(measures);
  } else if (state._modelTab === "focus") {
    pane.innerHTML = focusTables.length
      ? focusTables.map((t, i) => renderFocusTableCard(t, i)).join("")
      : `<div class="muted small">No matching table for this impact key in the model. Open <strong>All tables</strong>.</div>`;
  } else {
    pane.innerHTML = renderAllTablesPane(allTables);
  }

  // Wire SQL / copy / expand actions
  pane.querySelectorAll("[data-view-sql]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-view-sql");
      const pre = pane.querySelector(`[data-sql-full="${cssEsc(key)}"]`);
      if (pre) pre.classList.toggle("hidden");
      btn.textContent = pre && !pre.classList.contains("hidden") ? "Hide SQL" : "View full SQL";
    });
  });
  pane.querySelectorAll("[data-copy-sql]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.getAttribute("data-copy-sql");
      const pre = pane.querySelector(`[data-sql-full="${cssEsc(key)}"]`);
      const text = pre?.textContent || btn.getAttribute("data-sql-text") || "";
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Copied";
        setTimeout(() => { btn.textContent = "Copy SQL"; }, 1200);
      } catch (_) { /* ignore */ }
    });
  });
  pane.querySelectorAll("[data-toggle-cols]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-toggle-cols");
      const box = pane.querySelector(`[data-cols="${cssEsc(key)}"]`);
      if (!box) return;
      box.classList.toggle("hidden");
      btn.textContent = box.classList.contains("hidden") ? "Show columns" : "Hide columns";
    });
  });
  pane.querySelectorAll("[data-toggle-expr]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-toggle-expr");
      const box = pane.querySelector(`[data-expr="${cssEsc(key)}"]`);
      if (!box) return;
      box.classList.toggle("hidden");
      btn.textContent = box.classList.contains("hidden") ? "Show expression" : "Hide expression";
    });
  });
}

function renderFocusTableCard(t, idx) {
  const key = `f${idx}`;
  const st = t.sourceTypeLabel || "Unknown";
  const sql = t.sqlQuery || "";
  const url = t.sourceUrl || "";
  const file = t.fileName || "";
  const server = t.serverName || "";
  const srcTables = (t.sqlSourceTables || []).join(", ");
  const cols = t.columns || [];
  const meas = t.measures || [];

  let sourceBlock = "";
  if (sql) {
    sourceBlock = `
      <div class="source-block">
        <div class="source-label">SQL query</div>
        <div class="sql-preview mono">${escapeHtml(sql.slice(0, 220))}${sql.length > 220 ? "…" : ""}</div>
        <div class="sql-actions">
          <button type="button" class="btn primary sm" data-view-sql="${key}">View full SQL</button>
          <button type="button" class="btn secondary sm" data-copy-sql="${key}">Copy SQL</button>
        </div>
        <pre class="sql-full mono hidden" data-sql-full="${key}">${escapeHtml(sql)}</pre>
      </div>`;
  } else if (url) {
    sourceBlock = `
      <div class="source-block">
        <div class="source-label">File / SharePoint source</div>
        ${file ? `<div class="mono small" style="margin-bottom:6px">${escapeHtml(file)}</div>` : ""}
        <a class="source-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">Open source link</a>
      </div>`;
  } else if (t.sourceExpression) {
    sourceBlock = `
      <div class="source-block">
        <div class="source-label">Power Query / expression</div>
        <button type="button" class="btn secondary sm" data-toggle-expr="${key}">Show expression</button>
        <pre class="sql-full mono hidden" data-expr="${key}">${escapeHtml(t.sourceExpression)}</pre>
      </div>`;
  } else {
    sourceBlock = `<div class="muted small">No SQL or file link stored for this table in the catalog.</div>`;
  }

  const measHtml = meas.length
    ? `<div class="source-block"><div class="source-label">Measures on this table (${meas.length})</div>
        <ul class="measure-list">${meas.slice(0, 40).map((m) => `
          <li><strong>${escapeHtml(m.name || "—")}</strong>
            ${m.expression ? `<pre class="dax-snip mono">${escapeHtml(String(m.expression).slice(0, 280))}${String(m.expression).length > 280 ? "…" : ""}</pre>` : ""}
          </li>`).join("")}
        </ul></div>`
    : "";

  return `
    <div class="focus-card">
      <div class="focus-card-head">
        <strong>${escapeHtml(t.name || "—")}</strong>
        <span class="pill ${sourceTypePillClass(st)}">${escapeHtml(st)}</span>
      </div>
      <div class="kv-grid compact">
        <div class="k">Server / path</div><div class="v mono small">${escapeHtml(server || "—")}</div>
        <div class="k">Source tables / file</div><div class="v mono small">${escapeHtml(srcTables || file || "—")}</div>
        <div class="k">Columns</div><div class="v">${fmt(cols.length)}
          ${cols.length ? `<button type="button" class="btn ghost sm" data-toggle-cols="${key}" style="margin-left:8px">Show columns</button>` : ""}
        </div>
      </div>
      <div class="cols-box hidden" data-cols="${key}">
        <table class="mini-table"><thead><tr><th>Column</th><th>Type</th></tr></thead>
        <tbody>${cols.map((c) => `<tr><td>${escapeHtml(c.name || "")}</td><td class="mono small">${escapeHtml(c.dataType || "")}</td></tr>`).join("")}</tbody></table>
      </div>
      ${sourceBlock}
      ${measHtml}
    </div>`;
}

function renderAllTablesPane(tables) {
  if (!tables.length) return `<div class="muted small">No tables in catalog for this model.</div>`;
  const rows = tables.map((t, idx) => {
    const st = t.sourceTypeLabel || "Unknown";
    const sql = t.sqlQuery || "";
    const url = t.sourceUrl || "";
    const file = t.fileName || "";
    const key = `a${idx}`;
    let qCell = `<span class="muted">—</span>`;
    if (sql) {
      qCell = `<div class="sql-preview mono">${escapeHtml(sql.slice(0, 100))}${sql.length > 100 ? "…" : ""}</div>
        <div class="sql-actions">
          <button type="button" class="btn primary sm" data-view-sql="${key}">View full SQL</button>
          <button type="button" class="btn secondary sm" data-copy-sql="${key}">Copy SQL</button>
        </div>
        <pre class="sql-full mono hidden" data-sql-full="${key}">${escapeHtml(sql)}</pre>`;
    } else if (url) {
      qCell = `${file ? `<div class="mono small">${escapeHtml(file)}</div>` : ""}
        <a class="source-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">Open source</a>`;
    } else if (t.sourceExpression) {
      qCell = `<button type="button" class="btn ghost sm" data-toggle-expr="${key}">Expression</button>
        <pre class="sql-full mono hidden" data-expr="${key}">${escapeHtml(t.sourceExpression)}</pre>`;
    }
    return `<tr class="${t.isFocus ? "is-focus-row" : ""}">
      <td><strong>${escapeHtml(t.name || "—")}</strong>
        <div class="muted small">${fmt(t.columnCount)} cols · ${fmt(t.measureCount)} measures</div></td>
      <td><span class="pill ${sourceTypePillClass(st)}">${escapeHtml(st)}</span></td>
      <td class="mono small">${escapeHtml(t.serverName || "—")}</td>
      <td class="mono small">${escapeHtml((t.sqlSourceTables || []).join(", ") || file || "—")}</td>
      <td class="sql-cell">${qCell}</td>
    </tr>`;
  }).join("");

  return `
    <div class="table-wrap model-table-wrap">
      <table class="model-lineage-table">
        <thead>
          <tr>
            <th>Model table</th>
            <th>Source</th>
            <th>Server / path</th>
            <th>Source object</th>
            <th>SQL / link</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="muted small" style="margin-top:8px">Highlighted rows match the impact table you opened from.</p>`;
}

function renderMeasuresPane(measures) {
  if (!measures.length) {
    return `<div class="muted small">No DAX measures in the catalog for this model.</div>`;
  }
  return `<ul class="measure-list full">${measures.map((m) => `
    <li>
      <div><strong>${escapeHtml(m.name || "—")}</strong>
        <span class="muted small"> · ${escapeHtml(m.table || "")}</span></div>
      ${m.expression ? `<pre class="dax-snip mono">${escapeHtml(m.expression)}</pre>` : ""}
    </li>`).join("")}</ul>`;
}

async function runLookup() {
  const qRaw = ($("#lookupInput").value || "").trim();
  const q = qRaw.toLowerCase();
  const box = $("#lookupResults");
  if (!q) {
    box.innerHTML = `<div class="muted">Enter an EDW or Power BI table name (e.g. FactSales or Sales).</div>`;
    return;
  }
  box.innerHTML = `<div class="muted small">Searching…</div>`;

  // Prefer already-loaded thin rows (instant); fall back to server lookup API
  let hits = state.rows
    .filter((r) =>
      (r.table || "").toLowerCase() === q ||
      (r.modelTableNames || []).some((m) => String(m).toLowerCase() === q) ||
      (r.searchText || "").includes(q)
    )
    .sort((a, b) => b.reportCount - a.reportCount)
    .slice(0, 30);

  if (!hits.length) {
    try {
      const res = await fetchJsonNoCache(
        `/api/catalog/impact/lookup?table=${encodeURIComponent(qRaw)}`,
        { timeoutMs: 120000 }
      );
      if (res.ok) {
        const body = await res.json();
        hits = (body.results || []).map((e) => {
          const s = e.impactSummary || {};
          return {
            tableKey: e.tableKey,
            table: e.table || "—",
            sourceType: e.sourceType || "Unknown",
            server: e.server || "",
            database: e.database || "",
            schema: e.schema || "",
            modelTableNames: e.modelTableNames || [],
            reportCount: s.reportCount || 0,
            datasetCount: s.datasetCount || 0,
            workspaceCount: s.workspaceCount || 0,
            searchText: "",
          };
        });
      }
    } catch (e) {
      console.warn("lookup API failed", e);
    }
  }

  if (!hits.length) {
    box.innerHTML = `<div class="muted">No matches for <strong>${escapeHtml(qRaw)}</strong>. Try the EDW name or a Power BI model name.</div>`;
    return;
  }
  box.innerHTML = `<div class="callout" style="margin-bottom:12px">Search matches <strong>source names</strong> and <strong>renamed Power BI names</strong>. Open a hit to see every affected report.</div>` + hits.map((r) => {
    const aliases = (r.modelTableNames || []).slice(0, 6).join(", ");
    return `
    <div class="lookup-hit">
      <h3>${escapeHtml(r.table)} <span class="pill ${sourceClass(r.sourceType)}">${escapeHtml(isPhysical(r) ? "Source / EDW" : "PBI name")}</span></h3>
      <div class="meta-line">${escapeHtml(r.server || "—")}${r.database ? " / " + escapeHtml(r.database) : ""} · <strong>${fmt(r.reportCount)}</strong> reports · ${fmt(r.datasetCount)} datasets</div>
      ${aliases ? `<div class="muted small" style="margin-bottom:8px">Power BI name(s): ${escapeHtml(aliases)}</div>` : ""}
      <button class="btn secondary sm" data-open="${escapeAttr(r.tableKey)}">Show affected reports</button>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openDrawer(b.getAttribute("data-open"))));
}

function exportCsv(rows, filename) {
  const headers = ["table", "sourceType", "server", "database", "schema", "reportCount", "datasetCount", "workspaceCount", "tableKey"];
  const lines = [headers.join(",")];
  for (const r of rows) {
    lines.push(headers.map((h) => csvEscape(r[h])).join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function csvEscape(v) {
  const s = v == null ? "" : String(v);
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function kpi(label, value, hint = "") {
  return `<div class="kpi"><div class="label">${escapeHtml(label)}</div><div class="value">${typeof value === "number" ? fmt(value) : escapeHtml(value)}</div>${hint ? `<div class="hint">${escapeHtml(hint)}</div>` : ""}</div>`;
}
function bar(label, value, max) {
  const w = Math.max(4, Math.round((value / max) * 100));
  return `<div class="bar-row"><div class="bar-label" title="${escapeAttr(label)}">${escapeHtml(label)}</div>
    <div class="bar-val">${fmt(value)}</div><div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div></div>`;
}
function bindBars(sel, rows) {
  // bars are static; click labels via delegation optional — open top table on row click
  const root = $(sel);
  root.querySelectorAll(".bar-row").forEach((el, i) => {
    el.style.cursor = "pointer";
    el.addEventListener("click", () => openDrawer(rows[i].tableKey));
  });
}
function countBy(arr, fn) {
  const o = {};
  for (const x of arr) { const k = fn(x); o[k] = (o[k] || 0) + 1; }
  return o;
}
function pct(n, d) { return d ? `${Math.round((n / d) * 100)}%` : "0%"; }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }

function renderCoverageBanner() {
  const stats = state.catalog?.stats || state.summary?.stats || {};
  const el = $("#coverageBanner");
  if (!el) return;
  el.innerHTML = `
    <div class="cov-item"><div class="cov-label">Workspaces</div><div class="cov-value">${fmt(stats.workspaceCount)}</div></div>
    <div class="cov-item"><div class="cov-label">Reports</div><div class="cov-value">${fmt(stats.reportCount)}</div></div>
    <div class="cov-item"><div class="cov-label">Datasets</div><div class="cov-value">${fmt(stats.datasetCount)}</div></div>
    <div class="cov-item"><div class="cov-label">Model tables</div><div class="cov-value">${fmt(stats.tableCount)}</div></div>
    <div class="cov-note">Tenant-wide Admin Scanner extract (personal workspaces excluded by default).
      Click a workspace → all reports → open a report for dataset, tables, and source lineage.</div>`;
}

function filteredWorkspaces() {
  const list = state.catalog?.workspaces || [];
  const q = ($("#wsSearch")?.value || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter((w) => `${w.name || ""} ${w.id || ""}`.toLowerCase().includes(q));
}

function renderWorkspaceList() {
  if (!state.catalog) {
    $("#wsList").innerHTML = `<div class="muted small">workspace_catalog.json missing. Run:<br/><code>python scripts/build_workspace_catalog.py</code></div>`;
    $("#wsCountBadge").textContent = "0";
    return;
  }
  const list = filteredWorkspaces();
  $("#wsCountBadge").textContent = fmt(list.length);
  $("#wsList").innerHTML = list.map((w) => `
    <button class="ws-item ${w.id === state.selectedWorkspaceId ? "active" : ""}" data-ws="${escapeAttr(w.id)}">
      <div class="name">${escapeHtml(w.name || w.id)}</div>
      <div class="meta">${fmt(w.reportCount)} reports · ${fmt(w.datasetCount)} datasets${w.state ? " · " + escapeHtml(w.state) : ""}</div>
    </button>`).join("") || `<div class="muted small">No workspaces match.</div>`;

  $("#wsList").querySelectorAll("[data-ws]").forEach((btn) => {
    btn.addEventListener("click", () => selectWorkspace(btn.getAttribute("data-ws")));
  });
}

function selectWorkspace(wsId) {
  state.selectedWorkspaceId = wsId;
  state.wsReportFilter = "";
  if ($("#wsReportSearch")) $("#wsReportSearch").value = "";
  renderWorkspaceList();
  renderWorkspaceDetail();
}

function getSelectedWorkspace() {
  return (state.catalog?.workspaces || []).find((w) => w.id === state.selectedWorkspaceId) || null;
}

function renderWorkspaceDetail() {
  const ws = getSelectedWorkspace();
  if (!ws) {
    $("#wsEmpty").classList.remove("hidden");
    $("#wsDetail").classList.add("hidden");
    return;
  }
  $("#wsEmpty").classList.add("hidden");
  $("#wsDetail").classList.remove("hidden");
  $("#wsDetailName").textContent = ws.name || ws.id;
  $("#wsDetailMeta").textContent = `ID: ${ws.id} · type: ${ws.type || "—"} · capacity: ${ws.isOnDedicatedCapacity ? "dedicated" : "shared"}`;
  $("#wsDetailKpis").innerHTML = [
    kpi("Reports", ws.reportCount || 0),
    kpi("Datasets", ws.datasetCount || 0),
    kpi("Dashboards", ws.dashboardCount || 0),
  ].join("");

  const q = (state.wsReportFilter || "").toLowerCase();
  const reports = (ws.reports || []).filter((r) =>
    !q || `${r.name || ""} ${r.datasetId || ""} ${r.reportType || ""}`.toLowerCase().includes(q)
  );
  const dsMap = Object.fromEntries((ws.datasets || []).map((d) => [d.id, d.name]));

  const tb = $("#wsReportsTable tbody");
  tb.innerHTML = reports.map((r) => {
    const dsName = dsMap[r.datasetId] || (state.catalog?.datasets?.[r.datasetId]?.name) || r.datasetId || "—";
    return `<tr>
      <td><button class="linkish" data-report="${escapeAttr(r.id)}" data-ws="${escapeAttr(ws.id)}">${escapeHtml(r.name || r.id)}</button></td>
      <td><span class="pill">${escapeHtml(r.reportType || "—")}</span></td>
      <td class="small">${escapeHtml(dsName)}</td>
      <td><button class="btn ghost sm" data-report="${escapeAttr(r.id)}" data-ws="${escapeAttr(ws.id)}">Details</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">No reports in this workspace.</td></tr>`;

  tb.querySelectorAll("[data-report]").forEach((btn) => {
    btn.addEventListener("click", () => openReportDrawer(btn.getAttribute("data-ws"), btn.getAttribute("data-report")));
  });
}

function sourceTypePillClass(label) {
  const x = (label || "").toLowerCase();
  if (x.includes("sql")) return "sql";
  if (x.includes("sharepoint") || x.includes("excel") || x.includes("file") || x.includes("folder") || x.includes("web")) return "odata";
  if (x.includes("expression")) return "as";
  if (x.includes("analysis") || x.includes("snow")) return "snow";
  return "model";
}

/** Render server/path cell — clickable when http(s) SharePoint/Web URL */
function formatServerCell(t) {
  const server = t.serverName || "N/A";
  const url = t.sourceUrl || (String(server).toLowerCase().startsWith("http") ? server : null);
  if (url && String(url).toLowerCase().startsWith("http")) {
    const label = server && server !== "N/A" ? server : url;
    return `<a class="source-link mono small" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer" title="Open source location">${escapeHtml(label)}</a>`;
  }
  return `<span class="mono small">${escapeHtml(server)}</span>`;
}

/** SQL tables OR Excel/SharePoint file name */
function formatSourceObjectCell(t) {
  const fileName = t.fileName || "";
  const tables = t.sqlSourceTables || [];
  const url = t.sourceUrl || "";
  const parts = [];
  if (fileName) {
    if (url && String(url).toLowerCase().startsWith("http")) {
      parts.push(`<a class="source-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(fileName)}</a>`);
    } else {
      parts.push(escapeHtml(fileName));
    }
  }
  for (const x of tables) {
    if (fileName && String(x).toLowerCase() === String(fileName).toLowerCase()) continue;
    parts.push(escapeHtml(x));
  }
  if (!parts.length) return "<span class='muted'>None</span>";
  return `<span class="small">${parts.join("<br/>")}</span>`;
}

function openReportDrawer(wsId, reportId) {
  const ws = (state.catalog?.workspaces || []).find((w) => w.id === wsId);
  if (!ws) return;
  const report = (ws.reports || []).find((r) => r.id === reportId);
  if (!report) return;
  const ds = report.datasetId ? state.catalog?.datasets?.[report.datasetId] : null;

  $("#reportDrawerTitle").textContent = report.name || report.id;
  $("#reportDrawerSub").textContent = `${ws.name || ws.id} · ${report.reportType || "Report"}`;

  const tables = ds?.tables || [];
  state._reportTables = tables; // for column modal + SQL actions

  const sqlCount = tables.filter((t) => (t.sourceTypeLabel || "").toLowerCase().includes("sql")).length;
  const exprCount = tables.filter((t) => (t.sourceTypeLabel || "").toLowerCase().includes("expression")).length;

  const rowsHtml = tables.map((t, idx) => {
    const st = t.sourceTypeLabel || "Unknown";
    const q = t.sqlQuery || "";
    const qPreview = q
      ? `<div class="sql-preview mono">${escapeHtml(q.slice(0, 140))}${q.length > 140 ? "…" : ""}</div>
         <div class="sql-actions">
           <button type="button" class="btn primary sm" data-view-sql="${idx}">View Full SQL</button>
           <button type="button" class="btn secondary sm" data-copy-sql="${idx}">Copy SQL</button>
         </div>`
      : (t.sourceUrl
          ? `<a class="source-link" href="${escapeAttr(t.sourceUrl)}" target="_blank" rel="noopener noreferrer">Open source</a>`
          : `<span class="muted">N/A</span>`);
    return `<tr>
      <td><strong>${escapeHtml(t.name || "—")}</strong>
        <div class="muted small">${fmt(t.columnCount)} cols · ${fmt(t.measureCount)} measures</div></td>
      <td><span class="pill ${sourceTypePillClass(st)}">${escapeHtml(st)}</span></td>
      <td>${formatServerCell(t)}</td>
      <td>${formatSourceObjectCell(t)}</td>
      <td class="sql-cell">${qPreview}</td>
      <td><button type="button" class="btn ghost sm" data-col-info="${idx}">Column Info</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No tables found for this dataset.</td></tr>`;

  $("#reportDrawerBody").innerHTML = `
    <div class="drawer-kpis" style="margin-bottom:14px">
      ${kpi("Model tables", tables.length)}
      ${kpi("SQL sources", sqlCount)}
      ${kpi("Expressions / other", tables.length - sqlCount)}
    </div>
    <div class="detail-block">
      <h3>Report</h3>
      <div class="kv-grid">
        <div class="k">Name</div><div class="v">${escapeHtml(report.name || "—")}</div>
        <div class="k">Workspace</div><div class="v">${escapeHtml(ws.name || "—")}</div>
        <div class="k">Dataset</div><div class="v">${escapeHtml(ds?.name || "—")}</div>
        <div class="k">Storage mode</div><div class="v">${escapeHtml(ds?.targetStorageMode || "—")}</div>
      </div>
    </div>
    <div class="detail-block">
      <div class="section-title-row">
        <h3>Model tables (source lineage)</h3>
        <input id="modelTableFilter" type="search" placeholder="Search tables…" class="drawer-search" style="max-width:220px;margin:0" />
      </div>
      <div class="table-wrap model-table-wrap">
        <table class="model-lineage-table" id="modelLineageTable">
          <thead>
            <tr>
              <th>Model table name</th>
              <th>Source type</th>
              <th>Server / path / link</th>
              <th>Source tables / file</th>
              <th>SQL query / open</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
      <p class="muted small" style="margin-top:10px">
        Same Admin Scanner API as impact extract. Column “used in report” = present in the semantic model bound to this report
        (visual-level usage needs Embed API and is not in Scanner).
      </p>
    </div>
    <div id="columnModal" class="column-modal hidden"></div>
    <div id="sqlModal" class="column-modal hidden"></div>`;

  $("#reportDrawer").classList.remove("hidden");
  $("#drawer").classList.add("hidden");
  $("#drawerBackdrop").classList.remove("hidden");
  $("#reportDrawer").setAttribute("aria-hidden", "false");

  // Wire table actions
  const body = $("#reportDrawerBody");
  body.querySelector("#modelTableFilter")?.addEventListener("input", (e) => {
    const q = (e.target.value || "").toLowerCase();
    body.querySelectorAll("#modelLineageTable tbody tr").forEach((tr) => {
      tr.style.display = !q || tr.innerText.toLowerCase().includes(q) ? "" : "none";
    });
  });
  body.querySelectorAll("[data-col-info]").forEach((btn) => {
    btn.addEventListener("click", () => openColumnInfoModal(Number(btn.getAttribute("data-col-info"))));
  });
  body.querySelectorAll("[data-view-sql]").forEach((btn) => {
    btn.addEventListener("click", () => openSqlModal(Number(btn.getAttribute("data-view-sql"))));
  });
  body.querySelectorAll("[data-copy-sql]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const t = state._reportTables?.[Number(btn.getAttribute("data-copy-sql"))];
      if (!t?.sqlQuery) return;
      await navigator.clipboard.writeText(t.sqlQuery);
      btn.textContent = "Copied";
      setTimeout(() => { btn.textContent = "Copy SQL"; }, 1200);
    });
  });
}

function openColumnInfoModal(tableIndex) {
  const t = state._reportTables?.[tableIndex];
  if (!t) return;
  const cols = t.columns || [];
  const rows = cols.map((c) => {
    const used = c.usedInReport !== false;
    const usedIn = (c.usedIn || []).join(", ") || "Model";
    return `<tr>
      <td>${escapeHtml(c.name || "—")}</td>
      <td class="mono small">${escapeHtml(c.dataType || "—")}</td>
      <td>${used ? "<span class='pill sql'>✓ Used</span>" : "<span class='muted'>—</span>"}</td>
      <td class="small">${escapeHtml(usedIn)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">No columns in scan for this table.</td></tr>`;

  const modal = $("#columnModal");
  modal.classList.remove("hidden");
  modal.innerHTML = `
    <div class="column-modal-card">
      <div class="column-modal-head">
        <div>
          <div class="eyebrow">Column Info</div>
          <h3 style="margin:4px 0 0">${escapeHtml(t.name || "Table")}</h3>
        </div>
        <button type="button" class="icon-btn" id="closeColumnModal">✕</button>
      </div>
      <div class="table-wrap" style="max-height:55vh;overflow:auto">
        <table>
          <thead>
            <tr>
              <th>Column name</th>
              <th>Data type</th>
              <th>Used in report</th>
              <th>Used in</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  modal.querySelector("#closeColumnModal")?.addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
}

function openSqlModal(tableIndex) {
  const t = state._reportTables?.[tableIndex];
  if (!t?.sqlQuery) return;
  const modal = $("#sqlModal");
  modal.classList.remove("hidden");
  modal.innerHTML = `
    <div class="column-modal-card">
      <div class="column-modal-head">
        <div>
          <div class="eyebrow">Full SQL</div>
          <h3 style="margin:4px 0 0">${escapeHtml(t.name || "")}</h3>
        </div>
        <button type="button" class="icon-btn" id="closeSqlModal">✕</button>
      </div>
      <pre class="sql-full mono">${escapeHtml(t.sqlQuery)}</pre>
      <button type="button" class="btn secondary sm" id="copySqlModal">Copy SQL</button>
    </div>`;
  modal.querySelector("#closeSqlModal")?.addEventListener("click", () => modal.classList.add("hidden"));
  modal.querySelector("#copySqlModal")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(t.sqlQuery);
  });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
}

function closeReportDrawer() {
  $("#reportDrawer").classList.add("hidden");
  $("#reportDrawer").setAttribute("aria-hidden", "true");
  // keep backdrop if table drawer still open
  if ($("#drawer").classList.contains("hidden")) {
    $("#drawerBackdrop").classList.add("hidden");
  }
}

function closeAllDrawers() {
  closeModelModal();
  closeDrawer();
  closeReportDrawer();
  $("#drawerBackdrop").classList.add("hidden");
}

function setView(name) {
  // Only Table impact + Impact lookup — top side-by-side tabs (no nested sidebar).
  const allowed = new Set(["tables", "lookup"]);
  if (!allowed.has(name)) name = "tables";

  document.querySelectorAll(".nav-item, .section-tab").forEach((b) => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    if (b.getAttribute("role") === "tab") b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  const target = $(`#view-${name}`);
  if (target) target.classList.remove("hidden");
  const titles = {
    tables: ["Table impact", "Search every source table → reports / datasets / workspaces"],
    lookup: ["Impact lookup", "If we change table X, which reports are affected?"],
  };
  const pair = titles[name] || titles.tables;
  // Title is the selected tab — avoid a second big heading under the tabs
  if ($("#viewTitle") && !$("#viewTitle").classList.contains("hidden")) {
    $("#viewTitle").textContent = pair[0];
  }
  if ($("#viewSubtitle")) $("#viewSubtitle").textContent = pair[1];

  // Export actions apply to the tables grid only
  const exportMode = name === "tables";
  if ($("#exportCsvBtn")) $("#exportCsvBtn").style.display = exportMode ? "" : "none";
  if ($("#exportFilteredBtn")) $("#exportFilteredBtn").style.display = exportMode ? "" : "none";
}

function wire() {
  // Match Control Center light chrome when running inside the app shell / iframe
  if (document.querySelector(".app.embedded")) {
    document.documentElement.classList.add("impact-embedded");
    document.body.classList.add("impact-embedded");
  }

  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  ["searchInput", "sourceFilter", "minReports", "resolutionFilter"].forEach((id) => {
    const el = $(`#${id}`);
    if (!el) return;
    el.addEventListener("input", applyFilters);
    el.addEventListener("change", applyFilters);
  });
  $("#clearFilters")?.addEventListener("click", () => {
    if ($("#searchInput")) $("#searchInput").value = "";
    if ($("#sourceFilter")) $("#sourceFilter").value = "";
    if ($("#minReports")) $("#minReports").value = "1";
    if ($("#resolutionFilter")) $("#resolutionFilter").value = "";
    applyFilters();
  });
  $("#prevPage")?.addEventListener("click", () => { state.page--; renderTable(); });
  $("#nextPage")?.addEventListener("click", () => { state.page++; renderTable(); });
  $("#impactTable thead")?.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sortKey === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortKey = key; state.sortDir = key.endsWith("Count") ? "desc" : "asc"; }
    sortFiltered();
    renderTable();
  });
  $("#closeDrawer")?.addEventListener("click", closeDrawer);
  $("#closeReportDrawer")?.addEventListener("click", closeReportDrawer);
  $("#closeModelModal")?.addEventListener("click", closeModelModal);
  $("#closeModelModalBtn")?.addEventListener("click", closeModelModal);
  $("#modelModal")?.addEventListener("click", (e) => {
    if (e.target === $("#modelModal")) closeModelModal();
  });
  $("#drawerBackdrop")?.addEventListener("click", () => {
    // Backdrop closes model modal first, then drawers
    if ($("#modelModal") && !$("#modelModal").classList.contains("hidden")) {
      closeModelModal();
      return;
    }
    closeAllDrawers();
  });
  $("#drawerReportFilter")?.addEventListener("input", renderDrawerReports);
  $("#copyReportsBtn")?.addEventListener("click", async () => {
    const names = state.drawerReports.map((r) => r.reportName).filter(Boolean).join("\n");
    await navigator.clipboard.writeText(names);
    $("#copyReportsBtn").textContent = "Copied";
    setTimeout(() => { if ($("#copyReportsBtn")) $("#copyReportsBtn").textContent = "Copy names"; }, 1200);
  });
  $("#wsSearch")?.addEventListener("input", renderWorkspaceList);
  $("#wsReportSearch")?.addEventListener("input", (e) => {
    state.wsReportFilter = e.target.value || "";
    renderWorkspaceDetail();
  });
  $("#lookupBtn")?.addEventListener("click", runLookup);
  $("#lookupInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") runLookup(); });
  $("#exportCsvBtn")?.addEventListener("click", () => exportCsv(state.rows, "impact_all_tables.csv"));
  $("#exportFilteredBtn")?.addEventListener("click", () => exportCsv(state.filtered, "impact_filtered_tables.csv"));
  $("#reloadBtn")?.addEventListener("click", () => loadData(true));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("#modelModal") && !$("#modelModal").classList.contains("hidden")) {
      closeModelModal();
      return;
    }
    closeAllDrawers();
  });
}

/** Sync theme from Control Center host (or localStorage). */
function applyHostTheme(theme) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
}
try {
  const saved = localStorage.getItem("pbi_cc_theme");
  if (saved === "light" || saved === "dark") applyHostTheme(saved);
} catch (_) { /* ignore */ }
window.addEventListener("message", (ev) => {
  try {
    if (ev.origin !== window.location.origin) return;
    const data = ev.data || {};
    if (data.type === "pbi-cc-theme") applyHostTheme(data.theme);
  } catch (_) { /* ignore */ }
});

wire();
loadData(false);

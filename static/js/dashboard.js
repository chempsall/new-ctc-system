/**
 * dashboard.js
 * Resource Forecast — Dashboard
 *
 * Loads the summary JSON once on page load. All filtering, sorting,
 * and drill-down happens client-side against the in-memory data.
 * Zero additional server requests for any dashboard interaction.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  summary:        null,    // Full summary payload from /api/summary
  activePeriod:   null,    // Currently selected month label e.g. "Apr-2026"
  activeView:     "staff", // "staff" | "projects"
  filters: {
    job_function: "all",
    job_title:    "all",
    department:   "all",
    horizon:      "all",   // "all" | "linked" | "norecord"
    search:       "",
  },
  selectedStaff:    null,  // horizon_person_number of selected row
  selectedProject:  null,  // project_id of selected row
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------
const fmt = {
  days: d => {
    if (d === null || d === undefined) return "—";
    const n = parseFloat(d);
    if (isNaN(n)) return "—";
    return n % 1 === 0 ? n.toString() : n.toFixed(1);
  },
  currency: n => {
    if (n === null || n === undefined) return "—";
    return new Intl.NumberFormat("en-GB", {
      style: "currency", currency: "GBP", maximumFractionDigits: 0
    }).format(n);
  },
  multiplier: n => {
    if (n === null || n === undefined) return "—";
    return parseFloat(n).toFixed(3) + "×";
  },
  percent: n => {
    if (n === null || n === undefined) return "—";
    const pct = (parseFloat(n) * 100).toFixed(1);
    return pct + "%";
  },
  initials: name => {
    if (!name) return "?";
    const parts = name.split(",").map(s => s.trim());
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase();
  },
  gradeShort: job_title => {
    if (!job_title) return "";
    const m = job_title.match(/^(P\d|L\d|T\d)/);
    return m ? m[1] : job_title.slice(0, 3);
  },
};

// ---------------------------------------------------------------------------
// Load data
// ---------------------------------------------------------------------------
async function loadSummary() {
  setLoadingStatus("Loading resource data...");
  try {
    const resp = await fetch("/api/summary");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.summary = data;
    state.activePeriod = data.periods[0];
    return true;
  } catch (e) {
    setLoadingStatus("Could not connect to server. Is it running?");
    console.error("Failed to load summary:", e);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Initialise UI
// ---------------------------------------------------------------------------
function init() {
  buildMonthTabs();
  buildFilterOptions();
  renderMetrics();
  renderConflictBanner();
  renderView();
  wireEvents();
  updateStatusBar();
}

function buildMonthTabs() {
  const container = document.getElementById("month-tabs");
  container.innerHTML = "";
  state.summary.periods.forEach((label, i) => {
    const btn = document.createElement("button");
    btn.className = "month-tab" + (i === 0 ? " active" : "");
    btn.textContent = label;
    btn.dataset.period = label;
    btn.addEventListener("click", () => selectPeriod(label));
    container.appendChild(btn);
  });
}

function buildFilterOptions() {
  const s = state.summary;

  // Teams — from unique values in staff
  const depts = [...new Set(s.staff.map(p => p.department).filter(Boolean))].sort();
  populateSelect("filter-department", depts, "All departments");

  // Job Titles — from unique values in staff
  const gradeOrder = t => {
    const m = t.match(/^([LPT])(\d+)/);
    if (!m) return "999";

    const letterOrder = { L: '1', P: '2', T: '3' }[m[1]] || '9';

    const n = parseInt(m[2], 10);
    const num = String(99 - n).padStart(2, '0');  // supports 1–99

    return letterOrder + num;
  };

  const titles = [...new Set(s.staff
    .map(p => p.job_title)
    .filter(Boolean)
  )].sort((a, b) => gradeOrder(a).localeCompare(gradeOrder(b)));


  populateSelect("filter-job-title", titles, "All job titles");

  // Disciplines
  const funcs = [...new Set(s.staff.map(p => p.job_function).filter(Boolean))].sort();
  populateSelect("filter-job-function", funcs, "All job functions");
}

function populateSelect(id, values, allLabel) {
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.innerHTML = `<option value="all">${allLabel}</option>`;
  values.forEach(v => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------
function renderMetrics() {
  const s  = state.summary;
  const p  = state.activePeriod;
  const staff    = filteredStaff();
  const projects = filteredProjects();

  const overCount = staff.filter(ps => ps.kpi[p] === "over").length;
  const noRecDays = staff.reduce((sum, ps) =>
    sum + (ps.no_record_days[p] || 0), 0);
  const conflicts = s.conflicts ? s.conflicts.length : 0;
  const noRecProj = projects.filter(pr => pr.horizon_status !== "linked").length;

  document.getElementById("metric-staff").textContent  = staff.length;
  document.getElementById("metric-projects").textContent = projects.length;
  document.getElementById("metric-over").textContent   = overCount;
  document.getElementById("metric-norec").textContent  = noRecProj;

  document.getElementById("metric-over-card").className =
    "metric-card" + (overCount > 0 ? " metric-card--alert" : "");
  document.getElementById("metric-norec-card").className =
    "metric-card" + (noRecProj > 0 ? " metric-card--warn" : "");
}

// ---------------------------------------------------------------------------
// Conflict banner
// ---------------------------------------------------------------------------
function renderConflictBanner() {
  const banner = document.getElementById("conflict-banner");
  const count  = state.summary.conflicts ? state.summary.conflicts.length : 0;
  if (count === 0) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  document.getElementById("conflict-count").textContent = count;
}

// ---------------------------------------------------------------------------
// Filtered data
// ---------------------------------------------------------------------------
function filteredStaff() {
  const f = state.filters;
  const p = state.activePeriod;
  return state.summary.staff.filter(person => {
    if (f.department !== "all" && person.department !== f.department) return false;
    if (f.job_title !== "all" && person.job_title !== f.job_title) return false;
    if (f.job_function !== "all" && person.job_function !== f.job_function) return false;
    if (f.search) {
      const q = f.search.toLowerCase();
      if (!person.name.toLowerCase().includes(q) &&
          !person.job_title.toLowerCase().includes(q)) return false;
    }
    return true;
  }).sort((a, b) => {
    // Over first, then check, then ok
    const order = { over: 0, check: 1, ok: 2, unavailable: 3 };
    const ka = order[a.kpi[p]] ?? 9;
    const kb = order[b.kpi[p]] ?? 9;
    if (ka !== kb) return ka - kb;
    return a.name.localeCompare(b.name);
  });
}

function filteredProjects() {
  const f = state.filters;
  const p = state.activePeriod;
  return state.summary.projects.filter(proj => {
    if (f.department !== "all" && proj.department !== f.department) return false;
    if (f.horizon !== "all") {
      if (f.horizon === "linked"   && proj.horizon_status !== "linked")   return false;
      if (f.horizon === "norecord" && proj.horizon_status === "linked")   return false;
    }
    if (f.search) {
      const q = f.search.toLowerCase();
      if (!proj.name.toLowerCase().includes(q) &&
          !(proj.number || "").toLowerCase().includes(q) &&
          !(proj.pm || "").toLowerCase().includes(q)) return false;
    }
    // Only show projects with allocation in this period (or all if no filter)
    return true;
  }).sort((a, b) => {
    // No-record first, then by days descending
    if (a.horizon_status !== b.horizon_status) {
      return a.horizon_status === "linked" ? 1 : -1;
    }
    const da = a.total_days[p] || 0;
    const db = b.total_days[p] || 0;
    return db - da;
  });
}

// ---------------------------------------------------------------------------
// Render main view
// ---------------------------------------------------------------------------
function renderView() {
  if (state.activeView === "staff") {
    renderStaffTable();
    document.getElementById("staff-panel").classList.remove("hidden");
    document.getElementById("projects-panel").classList.add("hidden");
  } else {
    renderProjectTable();
    document.getElementById("projects-panel").classList.remove("hidden");
    document.getElementById("staff-panel").classList.add("hidden");
  }
  renderMetrics();
}

// ---------------------------------------------------------------------------
// Staff table
// ---------------------------------------------------------------------------
function renderStaffTable() {
  const tbody = document.getElementById("staff-tbody");
  const staff = filteredStaff();
  const p     = state.activePeriod;
  const wdays = state.summary.working_days[p] || 20;

  if (staff.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5">
      <div class="empty-state">No staff match the current filters</div>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = staff.map(person => {
    const allocated = person.allocated[p] || 0;
    const capacity  = person.capacity[p]  || 0;
    const kpi       = person.kpi[p]       || "ok";
    const pct       = capacity > 0
      ? Math.min(100, (allocated / capacity) * 100).toFixed(1)
      : 0;
    const isSelected = String(person.id) === String(state.selectedStaff);

    return `<tr data-id="${person.id}" class="${isSelected ? "selected" : ""}">
      <td>
        <div class="staff-name">${escHtml(person.name)}</div>
        <div class="staff-grade">${escHtml(person.job_title)}</div>
      </td>
      <td>
        <span class="team-badge">${escHtml(person.job_function || "—")}</span>
      </td>
      <td>
        <div class="alloc-bar-wrap">
          <div class="alloc-bar">
            <div class="alloc-bar__fill alloc-bar__fill--${kpi}"
                 style="width:${pct}%"></div>
          </div>
          <span class="alloc-days">${fmt.days(allocated)}d</span>
        </div>
      </td>
      <td class="right">
        <span class="mono" style="font-size:11px;color:var(--text-tertiary)">
          / ${fmt.days(capacity)}d
        </span>
      </td>
      <td><span class="kpi kpi--${kpi}">${kpi}</span></td>
    </tr>`;
  }).join("");

  // Re-attach row click listeners
  tbody.querySelectorAll("tr[data-id]").forEach(row => {
    row.addEventListener("click", () => selectStaff(row.dataset.id));
  });
}

// ---------------------------------------------------------------------------
// Project table
// ---------------------------------------------------------------------------
function renderProjectTable() {
  const tbody    = document.getElementById("project-tbody");
  const projects = filteredProjects();
  const p        = state.activePeriod;

  if (projects.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6">
      <div class="empty-state">No projects match the current filters</div>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = projects.map(proj => {
    const days       = proj.total_days[p] || 0;
    const linked     = proj.horizon_status === "linked";
    const isSelected = String(proj.project_id) === String(state.selectedProject);
    const conflict   = proj.conflict_flag
      ? `<span title="Filename conflict" style="color:var(--amber-dark);margin-left:4px">⚠</span>`
      : "";

    return `<tr data-id="${proj.project_id}" class="${isSelected ? "selected" : ""}">
      <td>
        <div class="proj-number">${escHtml(proj.number || "—")}</div>
      </td>
      <td>
        <div class="proj-name">${escHtml(proj.name)}${conflict}</div>
        <div class="proj-task">${escHtml(proj.task_name || "")}</div>
      </td>
      <td><span class="team-badge">${escHtml(proj.department || "—")}</span></td>
      <td>
        <span class="horizon horizon--${linked ? "linked" : "norecord"}">
          <span class="horizon--dot"></span>
          ${linked ? "Linked" : "No record"}
        </span>
      </td>
      <td style="font-size:11px;color:var(--text-secondary)">
        ${escHtml(proj.pm || "—")}
      </td>
      <td class="right">
        <span class="mono" style="font-size:11px">
          ${fmt.days(days)}d
        </span>
      </td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(row => {
    row.addEventListener("click", () => selectProject(parseInt(row.dataset.id)));
  });
}

// ---------------------------------------------------------------------------
// Detail panel — staff
// ---------------------------------------------------------------------------
function selectStaff(id) {
  const person = state.summary.staff.find(p => String(p.id) === String(id));
  if (!person) return;

  state.selectedStaff   = id;
  state.selectedProject = null;
  renderView();
  showStaffDetail(person);
}

function showStaffDetail(person) {
  const panel = document.getElementById("detail-panel");
  const p     = state.activePeriod;
  const alloc = person.allocated[p]    || 0;
  const cap   = person.capacity[p]     || 0;
  const hdays = person.horizon_days[p] || 0;
  const ndays = person.no_record_days[p] || 0;
  const kpi   = person.kpi[p]          || "ok";

  document.getElementById("dp-avatar").textContent  = fmt.initials(person.name);
  document.getElementById("dp-name").textContent    = person.name;
  document.getElementById("dp-role").textContent    =
    `${person.job_title} · ${person.job_function || ""}`;

  document.getElementById("dp-stat-alloc").textContent    = fmt.days(alloc) + "d";
  document.getElementById("dp-stat-cap").textContent      = fmt.days(cap) + "d";
  document.getElementById("dp-stat-remain").textContent   = fmt.days(cap - alloc) + "d";
  document.getElementById("dp-stat-remain").style.color   =
    (cap - alloc) < 0 ? "var(--red)" : "var(--green-dark)";

  // KPI badge
  document.getElementById("dp-kpi").className = `kpi kpi--${kpi}`;
  document.getElementById("dp-kpi").textContent = kpi;

  // No-record warning
  const warnEl = document.getElementById("dp-norec-warn");
  if (ndays > 0) {
    warnEl.classList.remove("hidden");
    document.getElementById("dp-norec-days").textContent = fmt.days(ndays);
  } else {
    warnEl.classList.add("hidden");
  }

  // Projects breakdown
  const projContainer = document.getElementById("dp-projects");
  const projectLookup = Object.fromEntries(
    state.summary.projects.map(pr => [pr.project_id, pr])
  );

  const rows = person.projects
    .filter(pr => (pr.days[p] || 0) > 0)
    .sort((a, b) => (b.days[p] || 0) - (a.days[p] || 0));

  if (rows.length === 0) {
    projContainer.innerHTML =
      `<div class="empty-state" style="padding:16px">No allocations this period</div>`;
  } else {
    projContainer.innerHTML = rows.map(pr => {
      const proj   = projectLookup[pr.project_id];
      const linked = proj && proj.horizon_status === "linked";
      const name   = proj ? proj.name : `Project ${pr.project_id}`;
      const days   = pr.days[p] || 0;
      return `<div class="detail-project-row">
        <span class="horizon horizon--${linked ? "linked" : "norecord"}" style="flex-shrink:0">
          <span class="horizon--dot"></span>
        </span>
        <span class="detail-proj-name" title="${escHtml(name)}">${escHtml(name)}</span>
        <span class="detail-proj-days">${fmt.days(days)}d</span>
      </div>`;
    }).join("");
  }

  panel.classList.add("open");
}

// ---------------------------------------------------------------------------
// Detail panel — project
// ---------------------------------------------------------------------------
function selectProject(id) {
  const proj = state.summary.projects.find(p => String(p.project_id) === String(id));
  if (!proj) return;

  state.selectedProject = id;
  state.selectedStaff   = null;
  renderView();
  showProjectDetail(proj);
}

function showProjectDetail(proj) {
  const panel   = document.getElementById("detail-panel");
  const p       = state.activePeriod;
  const linked  = proj.horizon_status === "linked";
  document.getElementById("dp-avatar").textContent = proj.number
    ? proj.number.slice(-4)
    : "PROJ";
  document.getElementById("dp-name").textContent = proj.name;
  document.getElementById("dp-role").textContent =
    `${proj.task_name || ""}`;

  const totalDays = Object.values(proj.total_days).reduce((a, b) => a + b, 0);
  document.getElementById("dp-stat-alloc").textContent  = fmt.days(proj.total_days[p]) + "d";
  document.getElementById("dp-stat-cap").textContent    = fmt.days(totalDays) + "d total";
  document.getElementById("dp-stat-remain").textContent = "—";

  document.getElementById("dp-kpi").className   = `horizon horizon--${linked ? "linked" : "norecord"}`;
  document.getElementById("dp-kpi").innerHTML   =
    `<span class="horizon--dot"></span>${linked ? "Linked" : "No record"}`;

  // No-record warning
  const warnEl = document.getElementById("dp-norec-warn");
  if (!linked) {
    warnEl.classList.remove("hidden");
    document.getElementById("dp-norec-days").textContent = "not fee-earning";
  } else {
    warnEl.classList.add("hidden");
  }

  const projContainer = document.getElementById("dp-projects");
  projContainer.innerHTML = "";

  // Show staff allocated to this project
  const staffLookup = Object.fromEntries(
    state.summary.staff.map(s => [s.id, s])
  );
  const allocated = state.summary.staff
    .filter(s => s.projects.some(pr => pr.project_id === proj.project_id &&
                                        (pr.days[p] || 0) > 0))
    .map(s => ({
      name: s.name,
      job_title: s.job_title,
      days: (s.projects.find(pr => pr.project_id === proj.project_id)?.days[p] || 0)
    }))
    .sort((a, b) => b.days - a.days);

  if (allocated.length > 0) {
    projContainer.innerHTML += `
      <div style="margin-top:12px;font-size:10px;font-weight:600;
                  text-transform:uppercase;letter-spacing:0.08em;
                  color:var(--text-tertiary);margin-bottom:6px">
        Staff this period
      </div>
      ${allocated.map(s => `
        <div class="detail-project-row">
          <span class="team-badge">${fmt.gradeShort(s.job_title)}</span>
          <span class="detail-proj-name">${escHtml(s.name)}</span>
          <span class="detail-proj-days">${fmt.days(s.days)}d</span>
        </div>`).join("")}`;
  }

  panel.classList.add("open");
}

// ---------------------------------------------------------------------------
// Period selection
// ---------------------------------------------------------------------------
function selectPeriod(label) {
  state.activePeriod = label;
  document.querySelectorAll(".month-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.period === label);
  });
  renderView();
  if (state.selectedStaff) {
    const person = state.summary.staff.find(p => String(p.id) === String(state.selectedStaff));
    if (person) showStaffDetail(person);
  }
  if (state.selectedProject) {
    const proj = state.summary.projects.find(p => String(p.project_id) === String(state.selectedProject));
    if (proj) showProjectDetail(proj);
  }
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function switchView(view) {
  state.activeView = view;
  state.selectedStaff   = null;
  state.selectedProject = null;
  closeDetailPanel();

  document.querySelectorAll(".topnav__tab[data-view]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });

  // Show/hide horizon filter (only relevant for projects)
  const horizonFilter = document.getElementById("horizon-filter-wrap");
  if (horizonFilter) {
    horizonFilter.style.display = view === "projects" ? "" : "none";
  }

  // Show/hide job title + job function filters (only relevant for staff —
  // a project doesn't have a single job title or job function)
  const staffOnlyFilters = document.getElementById("staff-only-filters");
  if (staffOnlyFilters) {
    staffOnlyFilters.style.display = view === "staff" ? "" : "none";
  }

  renderView();
}

// ---------------------------------------------------------------------------
// Close detail panel
// ---------------------------------------------------------------------------
function closeDetailPanel() {
  document.getElementById("detail-panel").classList.remove("open");
  state.selectedStaff   = null;
  state.selectedProject = null;
  renderView();
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------
function updateStatusBar() {
  const s  = state.summary;
  const el = document.getElementById("status-text");
  if (!el || !s) return;

  const fmtDate = iso => {
    if (!iso) return null;
    return new Date(iso).toLocaleDateString("en-GB", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
    });
  };

  const imports = s.last_imports || {};
  const staffRun = fmtDate(imports.staff_list?.last_run);
  const parRun = fmtDate(imports.par_import?.last_run);
  const builtAt = fmtDate(s.generated_at);

  const parts = [];
  if (builtAt) parts.push(`Dashboard built <strong>${builtAt}</strong>`);
  if (staffRun) parts.push(`Staff list imported <strong>${staffRun}</strong>`);
  if (parRun) parts.push(`PAR imported <strong>${parRun}</strong>`);
  
  if (parts.length > 0) {
    el.innerHTML = parts.join(" &nbsp;&nbsp; ");
  } else {
    el.innerHTML = '<strong>${s.staff.length}</strong> staff · ' +
                   '<strong>${s.projects.length}</strong> projects';
  }
}

// ---------------------------------------------------------------------------
// Reset all filters back to defaults
// ---------------------------------------------------------------------------
function resetFilters() {
  state.filters.job_function = "all";
  state.filters.job_title    = "all";
  state.filters.department   = "all";
  state.filters.horizon      = "all";
  state.filters.search       = "";

  const deptSel  = document.getElementById("filter-department");
  const titleSel = document.getElementById("filter-job-title");
  const funcSel  = document.getElementById("filter-job-function");
  const horizonSel = document.getElementById("filter-horizon");
  const searchEl  = document.getElementById("filter-search");

  if (deptSel)    deptSel.value    = "all";
  if (titleSel)   titleSel.value   = "all";
  if (funcSel)    funcSel.value    = "all";
  if (horizonSel) horizonSel.value = "all";
  if (searchEl)   searchEl.value   = "";

  renderView();
}

// ---------------------------------------------------------------------------
// Wire up events
// ---------------------------------------------------------------------------
function wireEvents() {
  // Tab switching
  document.querySelectorAll(".topnav__tab[data-view]").forEach(btn => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  // Filters
  ["filter-department", "filter-job-title", "filter-job-function"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => {
      // Convert kebab-case id to camelCase filter key
      // filter-job-function -> job_function, filter-job-title -> job_title
      const key = id.replace("filter-", "").replace(/-/g, "_");
      state.filters[key] = el.value;
      renderView();
    });
  });

  const horizonSel = document.getElementById("filter-horizon");
  if (horizonSel) {
    horizonSel.addEventListener("change", () => {
      state.filters.horizon = horizonSel.value;
      renderView();
    });
  }

  const searchEl = document.getElementById("filter-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      state.filters.search = searchEl.value.trim();
      renderView();
    });
  }

  // Reset filters
  document.getElementById("filter-reset")?.addEventListener("click", resetFilters);

  // Close detail panel
  document.getElementById("dp-close")?.addEventListener("click", closeDetailPanel);

  // Click outside detail panel to close.
  // The row click listener (selectStaff/selectProject) calls renderView(),
  // which rebuilds tbody.innerHTML and opens the panel — all synchronously,
  // before this listener runs. By the time this runs, e.target may be a
  // detached node from the OLD table HTML, so .closest() against it always
  // fails even though the click genuinely landed on a row. Checking
  // composedPath() at dispatch time avoids this, since it captures the
  // event path before any DOM mutation happens.
  document.addEventListener("click", e => {
    const panel = document.getElementById("detail-panel");
    if (!panel.classList.contains("open")) return;
    if (panel.contains(e.target)) return;

    const clickedRow = e.composedPath().some(node =>
      node.nodeType === 1 &&
      node.tagName === "TR" &&
      node.dataset &&
      node.dataset.id !== undefined
    );
    if (clickedRow) return;

    closeDetailPanel();
  });
}

// ---------------------------------------------------------------------------
// Loading screen
// ---------------------------------------------------------------------------
function setLoadingStatus(msg) {
  const el = document.getElementById("loading-status");
  if (el) el.textContent = msg;
}

function hideLoading() {
  const el = document.getElementById("loading");
  if (el) el.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  const ok = await loadSummary();
  if (!ok) return;
  init();
  hideLoading();
});
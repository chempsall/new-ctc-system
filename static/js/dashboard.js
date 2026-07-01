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
  activeView:     "rtcs", // "rtcs" | "staff" | "projects"
  filters: {
    job_function: "all",
    job_title:    "all",
    department:   "all",
    horizon:      "all",   // "all" | "linked" | "norecord"
    search:       "",
  },
  selectedStaff:    null,  // horizon_person_number of selected row
  selectedProject:  null,  // project_id of selected row
  selectedRtc:      null,  // rtc_id of selected row
  rtcs:             [],    // loaded separately from /api/rtcs
  rtcFilters: {
    pm:       "",
    pd:       "",
    status:   "",
  },
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
  renderView();
  wireEvents();
  updateStatusBar();
  loadRtcs();
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
    document.getElementById("rtcs-panel").classList.add("hidden");
  } else if (state.activeView === "projects") {
    renderProjectTable();
    document.getElementById("projects-panel").classList.remove("hidden");
    document.getElementById("staff-panel").classList.add("hidden");
    document.getElementById("rtcs-panel").classList.add("hidden");
  } else {
    renderRtcTable();
    document.getElementById("rtcs-panel").classList.remove("hidden");
    document.getElementById("staff-panel").classList.add("hidden");
    document.getElementById("projects-panel").classList.add("hidden");
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
// RTCs — load, filter, render, select
// ---------------------------------------------------------------------------

async function loadRtcs() {
  const dept     = state.filters.department !== "all" ? state.filters.department : "";
  const search   = state.filters.search || "";
  const pm       = state.rtcFilters.pm  || "";
  const pd       = state.rtcFilters.pd  || "";
  const includeArchived = state.rtcFilters.status === "archived" ? "1" : "0";

  const params = new URLSearchParams();
  if (dept)                 params.set("department", dept);
  if (pm)                   params.set("pm", pm);
  if (pd)                   params.set("pd", pd);
  if (search)               params.set("search", search);
  if (includeArchived === "1") params.set("archived", "1");

  try {
    const r = await fetch(`/api/rtcs?${params}`);
    state.rtcs = await r.json();
    renderRtcTable();
  } catch(e) {
    console.error("Failed to load RTCs:", e);
  }
}

function filteredRtcs() {
  const statusFilter = state.rtcFilters.status;
  if (!statusFilter) return state.rtcs;
  return state.rtcs.filter(r => r.status === statusFilter);
}

function renderRtcTable() {
  const tbody  = document.getElementById("rtc-tbody");
  const rtcs   = filteredRtcs();
  const dupBtn = document.getElementById("btn-duplicate-rtc");

  if (rtcs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7">
      <div class="empty-state">No RTCs found</div>
    </td></tr>`;
    return;
  }

  const fmtDate = iso => {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit" });
  };

  const statusBadge = status => {
    const map = {
      active:        ["rtc-badge rtc-badge--active",  "Active"],
      needs_review:  ["rtc-badge rtc-badge--review",  "Needs review"],
      archived:      ["rtc-badge rtc-badge--archived", "Archived"],
    };
    const [cls, label] = map[status] || ["rtc-badge", status];
    return `<span class="${cls}">${label}</span>`;
  };

  tbody.innerHTML = rtcs.map(r => {
    const isSelected = String(r.rtc_id) === String(state.selectedRtc);
    const projName = escHtml(r.project_name || "No project name");
    const taskName = escHtml(r.task_name || "");
    const dept     = escHtml(r.department || "—");
    const pm       = escHtml(r.project_manager || "—");
    const days     = r.current_month_days
                     ? fmt.days(r.current_month_days) + "d"
                     : "—";

    return `<tr data-id="${r.rtc_id}" class="${isSelected ? "selected" : ""}">
      <td>
        <div class="proj-name">${projName}</div>
        ${taskName ? `<div class="proj-task">${taskName}</div>` : ""}
        <div class="proj-number">${escHtml(r.project_number || "")} ${escHtml(r.task_order_number || "")}</div>
      </td>
      <td><span class="team-badge">${dept}</span></td>
      <td>${pm}</td>
      <td>${statusBadge(r.status)}</td>
      <td class="right mono">${days}</td>
      <td class="text-tertiary" style="font-size:11px">
        ${escHtml(r.last_updated_by || "—")}<br>
        <span style="color:var(--text-tertiary)">${fmtDate(r.last_updated_at)}</span>
      </td>
      <td class="text-tertiary" style="font-size:11px">
        ${escHtml(r.last_opened_by || "—")}<br>
        <span style="color:var(--text-tertiary)">${fmtDate(r.last_opened)}</span>
      </td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(row => {
    row.addEventListener("click", () => selectRtc(parseInt(row.dataset.id)));
  });
}

function selectRtc(id) {
  const rtc = state.rtcs.find(r => r.rtc_id === id);
  if (!rtc) return;

  state.selectedRtc     = id;
  state.selectedStaff   = null;
  state.selectedProject = null;

  // Enable duplicate button since an RTC is now selected
  const dupBtn = document.getElementById("btn-duplicate-rtc");
  if (dupBtn) dupBtn.disabled = false;

  renderRtcTable();
  showRtcDetail(rtc);
}

function showRtcDetail(rtc) {
  const panel = document.getElementById("detail-panel");

  document.getElementById("dp-avatar").textContent = "RTC";
  document.getElementById("dp-name").textContent =
    rtc.project_name || "No project name";
  document.getElementById("dp-role").textContent =
    [rtc.task_name, rtc.department].filter(Boolean).join(" · ");

  // Stats: show current month days, future days, and last opened
  document.getElementById("dp-stat-alloc").textContent =
    fmt.days(rtc.current_month_days || 0) + "d";
  document.getElementById("dp-stat-cap").textContent =
    fmt.days(rtc.future_days || 0) + "d";
  document.getElementById("dp-stat-remain").textContent =
    rtc.last_opened ? new Date(rtc.last_opened).toLocaleDateString("en-GB", {
      day: "numeric", month: "short"
    }) : "Never";
  document.getElementById("dp-stat-remain").style.color = "";

  // Hide KPI badge and no-record warning (not applicable for RTCs)
  const kpiEl = document.getElementById("dp-kpi");
  if (kpiEl) kpiEl.className = "kpi kpi--ok"; kpiEl.textContent = "";
  const warnEl = document.getElementById("dp-norec-warn");
  if (warnEl) warnEl.classList.add("hidden");

  // Format start date as "July 2026" not "2026-07-01"
  const startFmt = rtc.start_date
    ? new Date(rtc.start_date + "T12:00:00").toLocaleDateString("en-GB", {
        month: "long", year: "numeric"
      })
    : "\u2014";

  // Project details
  const projContainer = document.getElementById("dp-projects");
  projContainer.innerHTML = `
    <div style="font-size:11px;line-height:1.8;color:var(--text-secondary)">
      <div><strong>Project</strong> ${escHtml(rtc.project_number || "\u2014")} / ${escHtml(rtc.task_order_number || "\u2014")}</div>
      <div><strong>PM</strong> ${escHtml(rtc.project_manager || "\u2014")}</div>
      <div><strong>PD</strong> ${escHtml(rtc.project_director || "\u2014")}</div>
      <div><strong>Start</strong> ${escHtml(startFmt)}</div>
      <div><strong>Created by</strong> ${escHtml(rtc.created_by || "\u2014")}</div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <a href="/rtc/${rtc.rtc_id}" class="btn-open-rtc">Open to edit \u2192</a>
        <button class="btn btn--sm btn--secondary"
                onclick="openRtcModal('duplicate')"
                style="font-size:11px">Duplicate</button>
      </div>
    </div>`;

  // Check for linkable Horizon record
  checkHorizonLink(rtc.rtc_id);

  // Relabel the stat headings for the RTC context
  const labels = document.querySelectorAll(".detail-stat__label");
  if (labels[0]) labels[0].textContent = "This month";
  if (labels[1]) labels[1].textContent = "Future days";
  if (labels[2]) labels[2].textContent = "Last opened";

  panel.classList.add("open");
}

// ---------------------------------------------------------------------------
// Detail panel — staff
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
  state.selectedRtc     = null;
  closeDetailPanel();

  document.querySelectorAll(".topnav__tab[data-view]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });

  // Show/hide horizon filter (only relevant for projects)
  const horizonFilter = document.getElementById("horizon-filter-wrap");
  if (horizonFilter) {
    horizonFilter.style.display = view === "projects" ? "" : "none";
  }

  // Show/hide job title + job function filters (only relevant for staff)
  const staffOnlyFilters = document.getElementById("staff-only-filters");
  if (staffOnlyFilters) {
    staffOnlyFilters.style.display = view === "staff" ? "" : "none";
  }

  // Show/hide RTC-specific filters
  const rtcOnlyFilters = document.getElementById("rtc-only-filters");
  if (rtcOnlyFilters) {
    rtcOnlyFilters.style.display = view === "rtcs" ? "" : "none";
  }

  // Month tabs only relevant for staff and projects views
  const monthTabs = document.getElementById("month-tabs");
  if (monthTabs) {
    monthTabs.style.display = view === "rtcs" ? "none" : "";
  }

  // Reload data fresh on every tab switch so changes made on one tab
  // are reflected immediately on the others without needing a page refresh
  if (view === "rtcs") {
    loadRtcs();
  } else {
    // Reload the summary cache for staff/projects views
    loadSummary().then(() => renderView());
    return; // renderView() called inside loadSummary chain above
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
  state.rtcFilters.pm        = "";
  state.rtcFilters.pd        = "";
  state.rtcFilters.status    = "";

  const deptSel  = document.getElementById("filter-department");
  const titleSel = document.getElementById("filter-job-title");
  const funcSel  = document.getElementById("filter-job-function");
  const horizonSel = document.getElementById("filter-horizon");
  const searchEl  = document.getElementById("filter-search");
  const pmSel     = document.getElementById("filter-rtc-pm");
  const pdSel     = document.getElementById("filter-rtc-pd");
  const statusSel = document.getElementById("filter-rtc-status");

  if (deptSel)    deptSel.value    = "all";
  if (titleSel)   titleSel.value   = "all";
  if (funcSel)    funcSel.value    = "all";
  if (horizonSel) horizonSel.value = "all";
  if (searchEl)   searchEl.value   = "";
  if (pmSel)      pmSel.value      = "";
  if (pdSel)      pdSel.value      = "";
  if (statusSel)  statusSel.value  = "";

  if (state.activeView === "rtcs") {
    loadRtcs();
  } else {
    renderView();
  }
}

// ---------------------------------------------------------------------------
// Horizon link check
// ---------------------------------------------------------------------------

async function checkHorizonLink(rtcId) {
  try {
    const r = await fetch(`/api/rtcs/${rtcId}/check-horizon`);
    const d = await r.json();
    if (!d.is_placeholder || !d.match) return;

    const details = document.getElementById("link-modal-details");
    if (details) {
      details.innerHTML = `
        <div class="form-lookup-row"><span class="form-lookup-label">Project</span><span class="form-lookup-value">${escHtml(d.match.project_name)}</span></div>
        <div class="form-lookup-row"><span class="form-lookup-label">Task</span><span class="form-lookup-value">${escHtml(d.match.task_name || "\u2014")}</span></div>
        <div class="form-lookup-row"><span class="form-lookup-label">PM</span><span class="form-lookup-value">${escHtml(d.match.project_manager || "\u2014")}</span></div>
        <div class="form-lookup-row"><span class="form-lookup-label">Number</span><span class="form-lookup-value">${escHtml(d.match.project_number)} / ${escHtml(d.match.task_order_number)}</span></div>`;
    }

    document.getElementById("link-modal-confirm").onclick = async () => {
      document.getElementById("link-modal-overlay").classList.add("hidden");
      await fetch(`/api/rtcs/${rtcId}/link-horizon`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_number:    d.match.project_number,
          task_order_number: d.match.task_order_number,
        })
      });
      await loadRtcs();
    };
    document.getElementById("link-modal-skip").onclick = () => {
      document.getElementById("link-modal-overlay").classList.add("hidden");
    };
    document.getElementById("link-modal-overlay").classList.remove("hidden");
  } catch(e) { /* silent fail — non-critical */ }
}

// ---------------------------------------------------------------------------
// Create / Duplicate RTC modal
// ---------------------------------------------------------------------------

let _rtcModalMode  = "create";
let _rtcPickerYear  = new Date().getFullYear();
let _rtcPickerMonth = null;

const MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun",
                    "Jul","Aug","Sep","Oct","Nov","Dec"];

function openRtcModal(mode) {
  _rtcModalMode = mode;
  document.getElementById("rtc-modal-title").textContent =
    mode === "duplicate" ? "Duplicate RTC" : "New RTC";
  document.getElementById("rtc-modal-submit").textContent =
    mode === "duplicate" ? "Duplicate RTC" : "Create RTC";
  document.getElementById("rtc-modal-submit").disabled = false;

  document.getElementById("rtc-proj-number").value = "";
  document.getElementById("rtc-task-number").value = "";
  if (document.getElementById("rtc-proj-name"))
    document.getElementById("rtc-proj-name").value = "";
  if (document.getElementById("rtc-task-name"))
    document.getElementById("rtc-task-name").value = "";
  document.getElementById("rtc-start-date").value  = "";
  document.getElementById("rtc-department").value  = "";
  document.getElementById("rtc-pd").value           = "";
  document.getElementById("rtc-pm").value           = "";
  document.getElementById("rtc-pm-external").checked = false;
  clearProjectLookup();
  document.getElementById("rtc-form-error").textContent = "";
  document.getElementById("rtc-form-error").classList.add("hidden");

  const deptSel = document.getElementById("rtc-department");
  deptSel.innerHTML = "<option value=\"\">Select cost centre\u2026</option>";
  (state.summary?.departments || []).forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.department; opt.textContent = d.department;
    deptSel.appendChild(opt);
  });

  // Clear PD/PM until a cost centre is chosen
  populatePersonDropdown("rtc-pd", null);
  populatePersonDropdown("rtc-pm", null);

  const now = new Date();
  _rtcPickerYear  = now.getFullYear();
  _rtcPickerMonth = now.getMonth() + 1;
  document.getElementById("rtc-start-date").value =
    `${_rtcPickerYear}-${String(_rtcPickerMonth).padStart(2,"0")}-01`;
  renderMonthPicker();

  // Wire cost centre change -> repopulate PD/PM
  document.getElementById("rtc-department").onchange = () => {
    const dept = document.getElementById("rtc-department").value;
    const external = document.getElementById("rtc-pm-external").checked;
    populatePersonDropdown("rtc-pd", dept);
    populatePersonDropdown("rtc-pm", external ? null : dept);
  };

  // Wire external PM checkbox
  document.getElementById("rtc-pm-external").onchange = () => {
    const dept = document.getElementById("rtc-department").value;
    const external = document.getElementById("rtc-pm-external").checked;
    populatePersonDropdown("rtc-pm", external ? null : dept);
  };

  document.getElementById("rtc-modal-overlay").classList.remove("hidden");
  document.getElementById("rtc-proj-number").focus();
}

// Populate a person dropdown filtered by department (null = all staff)
function populatePersonDropdown(selectId, department) {
  const sel = document.getElementById(selectId);
  const label = selectId === "rtc-pd" ? "Select project director\u2026" : "Select project manager\u2026";
  sel.innerHTML = `<option value="">${label}</option>`;

  const staff = state.summary?.staff || [];
  const filtered = department
    ? staff.filter(s => s.department === department)
    : staff;

  // Sort by name
  [...filtered].sort((a, b) => (a.name || "").localeCompare(b.name || "")).forEach(s => {
    const opt = document.createElement("option");
    opt.value = s.name; opt.textContent = s.name;
    sel.appendChild(opt);
  });
}

function closeRtcModal() {
  document.getElementById("rtc-modal-overlay").classList.add("hidden");
}

function renderMonthPicker() {
  document.getElementById("rtc-year-label").textContent = _rtcPickerYear;
  const grid = document.getElementById("rtc-month-grid");
  if (!grid) return;
  grid.innerHTML = MONTH_ABBR.map((name, i) => {
    const month = i + 1;
    const sel   = month === _rtcPickerMonth ? "selected" : "";
    return `<button type="button" class="month-picker__btn ${sel}" data-month="${month}">${name}</button>`;
  }).join("");
  grid.querySelectorAll(".month-picker__btn").forEach(btn => {
    btn.addEventListener("click", () => {
      _rtcPickerMonth = parseInt(btn.dataset.month);
      document.getElementById("rtc-start-date").value =
        `${_rtcPickerYear}-${String(_rtcPickerMonth).padStart(2,"0")}-01`;
      renderMonthPicker();
    });
  });
}

async function triggerProjectLookup(projNum, taskNum) {
  try {
    const r = await fetch(
      `/api/project?project_number=${encodeURIComponent(projNum)}&task_order_number=${encodeURIComponent(taskNum || "")}`
    );
    const d = await r.json();
    const result      = document.getElementById("rtc-lookup-result");
    const placeholder = document.getElementById("rtc-lookup-placeholder");
    const manualFields = document.getElementById("rtc-manual-fields");

    if (d.project_name) {
      document.getElementById("rtc-lookup-name").textContent = d.project_name      || "\u2014";
      document.getElementById("rtc-lookup-task").textContent = d.task_name         || "\u2014";
      document.getElementById("rtc-lookup-pm").textContent   = d.project_manager   || "\u2014";
      document.getElementById("rtc-lookup-pd").textContent   = d.project_director  || "\u2014";
      result.classList.remove("hidden");
      if (placeholder) placeholder.classList.add("hidden");
      if (manualFields) manualFields.classList.add("hidden");
      if (d.project_organisation) {
        const sel = document.getElementById("rtc-department");
        for (const opt of sel.options) {
          if (opt.value === d.project_organisation) { sel.value = opt.value; break; }
        }
      }
    } else {
      result.classList.add("hidden");
      if (placeholder) placeholder.classList.remove("hidden");
      if (manualFields) manualFields.classList.remove("hidden");
    }
  } catch(e) { console.error("Project lookup failed:", e); }
}

function clearProjectLookup() {
  document.getElementById("rtc-lookup-result")?.classList.add("hidden");
  document.getElementById("rtc-lookup-placeholder")?.classList.add("hidden");
  document.getElementById("rtc-manual-fields")?.classList.add("hidden");
}

async function submitRtcModal() {
  const projNum   = document.getElementById("rtc-proj-number").value.trim();
  const taskNum   = document.getElementById("rtc-task-number").value.trim();
  const startDate = document.getElementById("rtc-start-date").value;
  const dept      = document.getElementById("rtc-department").value;
  const projName  = document.getElementById("rtc-proj-name")?.value.trim() || "";
  const taskName  = document.getElementById("rtc-task-name")?.value.trim() || "";
  const pd        = document.getElementById("rtc-pd")?.value.trim() || "";
  const pm        = document.getElementById("rtc-pm")?.value.trim() || "";
  const errorEl   = document.getElementById("rtc-form-error");
  const submitBtn = document.getElementById("rtc-modal-submit");

  const errors = [];
  if (!projNum)   errors.push("Project number is required.");
  if (!startDate) errors.push("Start month is required.");
  if (!dept)      errors.push("Cost centre is required.");

  if (errors.length) {
    errorEl.textContent = errors.join(" ");
    errorEl.classList.remove("hidden");
    return;
  }

  errorEl.classList.add("hidden");
  submitBtn.disabled = true;
  submitBtn.textContent = "Saving\u2026";

  const body = {
    project_number:    projNum,
    task_order_number: taskNum || "",
    start_date:        startDate,
    department:        dept,
    project_name:      projName || undefined,
    task_name:         taskName || undefined,
    project_director:  pd       || undefined,
    project_manager:   pm       || undefined,
  };

  try {
    const url = _rtcModalMode === "duplicate"
                ? `/api/rtcs/${state.selectedRtc}/duplicate`
                : "/api/rtcs";
    const r   = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) {
      errorEl.textContent = d.error || "Server error \u2014 please try again.";
      errorEl.classList.remove("hidden");
      submitBtn.disabled = false;
      submitBtn.textContent = _rtcModalMode === "duplicate" ? "Duplicate RTC" : "Create RTC";
      return;
    }
    closeRtcModal();
    await loadRtcs();
    if (d.rtc_id) selectRtc(d.rtc_id);
  } catch(e) {
    errorEl.textContent = "Could not reach the server. Please try again.";
    errorEl.classList.remove("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = _rtcModalMode === "duplicate" ? "Duplicate RTC" : "Create RTC";
  }
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

  // RTC-specific filters
  document.getElementById("filter-rtc-pm")?.addEventListener("change", e => {
    state.rtcFilters.pm = e.target.value;
    if (state.activeView === "rtcs") loadRtcs();
  });
  document.getElementById("filter-rtc-pd")?.addEventListener("change", e => {
    state.rtcFilters.pd = e.target.value;
    if (state.activeView === "rtcs") loadRtcs();
  });
  document.getElementById("filter-rtc-status")?.addEventListener("change", e => {
    state.rtcFilters.status = e.target.value;
    renderRtcTable();
  });
  document.getElementById("filter-rtc-archived")?.addEventListener("change", e => {
    state.rtcFilters.archived = e.target.checked;
    if (state.activeView === "rtcs") loadRtcs();
  });

  // Also reload RTCs when the shared department/search filters change
  // (those already re-render staff/projects, but RTCs need a fresh fetch)
  const origSearchHandler = document.getElementById("filter-search")?._rtcHandler;
  document.getElementById("filter-search")?.addEventListener("input", () => {
    if (state.activeView === "rtcs") loadRtcs();
  });
  document.getElementById("filter-department")?.addEventListener("change", () => {
    if (state.activeView === "rtcs") loadRtcs();
  });

  // RTC action buttons
  document.getElementById("btn-create-rtc")?.addEventListener("click", () => {
    openRtcModal("create");
  });

  // Modal wiring
  document.getElementById("rtc-modal-close")?.addEventListener("click", closeRtcModal);
  document.getElementById("rtc-modal-cancel")?.addEventListener("click", closeRtcModal);
  document.getElementById("rtc-modal-overlay")?.addEventListener("click", e => {
    if (e.target.id === "rtc-modal-overlay") closeRtcModal();
  });
  document.getElementById("rtc-modal-submit")?.addEventListener("click", submitRtcModal);

  // Project lookup on blur
  const lookupTrigger = () => {
    const projNum = document.getElementById("rtc-proj-number")?.value.trim();
    const taskNum = document.getElementById("rtc-task-number")?.value.trim();
    if (projNum) triggerProjectLookup(projNum, taskNum || "");
    else clearProjectLookup();
  };
  document.getElementById("rtc-proj-number")?.addEventListener("blur", lookupTrigger);
  document.getElementById("rtc-task-number")?.addEventListener("blur", lookupTrigger);

  // Month picker year arrows
  document.getElementById("rtc-year-prev")?.addEventListener("click", () => {
    _rtcPickerYear--; renderMonthPicker();
  });
  document.getElementById("rtc-year-next")?.addEventListener("click", () => {
    _rtcPickerYear++; renderMonthPicker();
  });


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
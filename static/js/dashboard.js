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
  activeView: (["projects","staff","mgmt"].includes(window.location.hash.replace("#","")))
              ? window.location.hash.replace("#","")
              : "projects",
  filters: {
    job_function: "all",
    job_title:    "all",
    department:   "all",
    line_manager: "all",
    horizon:      "all",   // "all" | "linked" | "norecord"
    project_pm:   "all",
    project_pd:   "all",
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
  sort: {
    projects: { col: "this_month", dir: "desc" },
    staff:    { col: "status",     dir: "asc"  },
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
    const str = n % 1 === 0 ? n.toString() : parseFloat(n.toFixed(2)).toString();
    return str.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
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
    if (!state.activePeriod) state.activePeriod = data.periods[0];
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
  wireEvents();
  updateStatusBar();
  switchView(state.activeView);
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
  if (!state.activePeriod && state.summary.periods.length) {
    state.activePeriod = state.summary.periods[0];
  }

}

function buildFilterOptions() {
  const s = state.summary;

  // Teams — from unique values in staff
  const staffDepts    = (s.departments || []).map(d => d.department);
  const projectDepts  = (s.projects || []).map(p => p.department).filter(Boolean);
  const allDepts      = [...new Set([...staffDepts, ...projectDepts])].sort();
  const staffDeptsOnly = [...new Set(staffDepts)].sort();
  const deptList = state.activeView === "staff" ? staffDeptsOnly : allDepts;
  populateSelect("filter-department", deptList, "Department");

  // Line manager filter
  const managers = [...new Set(
    (s.staff || []).map(p => p.line_manager).filter(Boolean)
  )].sort();
  populateSelect("filter-line-manager", managers, "Line Manager");

  const deptWidth = document.getElementById("filter-department")?.offsetWidth;
  if (deptWidth) {
    const searchEl = document.getElementById("filter-search");
    if (searchEl) searchEl.style.width = deptWidth + "px";
  }

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


  populateSelect("filter-job-title", titles, "Job Title");

  // Disciplines
  const funcs = [...new Set(s.staff.map(p => p.job_function).filter(Boolean))].sort();
  populateSelect("filter-job-function", funcs, "Job Function");

  const pms = [...new Set((s.projects||[]).map(p => p.pm).filter(Boolean))].sort();
  populateSelect("filter-rtc-pm", pms, "Project Manager");

  populateSelect("filter-project-pm", pms, "Project Manager");

  const pds = [...new Set((s.projects||[]).map(p => p.director).filter(Boolean))].sort();
  populateSelect("filter-rtc-pd", pds, "Project Director");
  populateSelect("filter-project-pd", pds, "Project Director");
}

function populateSelect(id, values, allLabel) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const previous = sel.value;
  sel.innerHTML = `<option value="all">${allLabel}</option>`;
  values.forEach(v => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
  if (previous && [...sel.options].some(o => o.value === previous)) {
    sel.value = previous;
  }
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------
function renderMetrics() {
  const s  = state.summary;
  const p  = state.activePeriod;
  const staff    = filteredStaff();

  const overCount  = staff.filter(ps => !ps.id?.startsWith("GENERIC-") && ps.kpi[p] === "over").length;
  const underCount = staff.filter(ps => !ps.id?.startsWith("GENERIC-") && ps.kpi[p] === "under").length;
  const noRecProj = filteredRtcs().filter(r => r.horizon_status === "norecord").length;

const realStaff = staff.filter(ps => !ps.id?.startsWith("GENERIC-") && (ps.capacity?.[p] ?? 1) > 0);
  const fte = realStaff.reduce((sum, ps) => sum + (ps.fte?.[p] || 0), 0);
  document.getElementById("metric-staff").textContent    = realStaff.length;
  document.getElementById("metric-fte").textContent      = fte.toFixed(1);
  const allRtcs    = filteredRtcs();
  const activeRtcs = allRtcs.filter(r => (r.current_month_days || 0) > 0);
  document.getElementById("metric-projects").textContent = activeRtcs.length;
  const totalEl = document.getElementById("metric-projects-total");
  if (totalEl) totalEl.textContent = `${allRtcs.length} total`;
  document.getElementById("metric-over").textContent     = overCount;
  document.getElementById("metric-norec").textContent    = noRecProj;

  document.getElementById("metric-over-card").className =
    "metric-card" + (overCount > 0 ? " metric-card--alert" : "");
  document.getElementById("metric-norec-card").className =
    "metric-card" + (noRecProj > 0 ? " metric-card--warn" : "");
  document.getElementById("metric-under").textContent = underCount;
  document.getElementById("metric-under-card").className =
    "metric-card" + (underCount > 0 ? " metric-card--info" : "");
}

// ---------------------------------------------------------------------------
// Filtered data
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Sort helpers
// ---------------------------------------------------------------------------
function applySort(arr, view, getters) {
  const { col, dir } = state.sort[view];
  if (!col || !getters[col]) return arr;
  const mult = dir === "asc" ? 1 : -1;
  return [...arr].sort((a, b) => {
    const va = getters[col](a);
    const vb = getters[col](b);
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * mult;
    return String(va).localeCompare(String(vb)) * mult;
  });
}

function toggleSort(view, col) {
  const s = state.sort[view];
  if (s.col === col) {
    s.dir = s.dir === "asc" ? "desc" : "asc";
  } else {
    s.col = col;
    s.dir = "asc";
  }
  renderView();
  if (view === "projects") renderProjectTable();
}

function sortIndicator(view, col) {
  const s = state.sort[view];
  if (s.col !== col) return '<span class="sort-icon">⇅</span>';
  return `<span class="sort-icon sort-icon--active">${s.dir === "asc" ? "▲" : "▼"}</span>`;
}

function filteredStaff() {

  const f = state.filters;
  const p = state.activePeriod;
  // Build cascading line manager filter
  let lmFilter = null;
  if (f.line_manager !== "all" && f.line_manager) {
    const allStaff = state.summary.staff || [];
    const reportSet = new Set();
    const queue = [f.line_manager];
    while (queue.length) {
      const mgr = queue.shift();
      allStaff.filter(ps => ps.line_manager === mgr).forEach(ps => {
        if (!reportSet.has(ps.name)) {
          reportSet.add(ps.name);
          queue.push(ps.name);
        }
      });
    }
    lmFilter = reportSet;
  }

  const baseStaff = state.summary.staff.filter(person => {
    if (f.department !== "all" && person.department !== f.department) return false;
    if (f.job_title !== "all" && person.job_title !== f.job_title) return false;
    if (f.job_function !== "all" && person.job_function !== f.job_function) return false;
    if (lmFilter && !lmFilter.has(person.name)) return false;
    if (f.search) {
      const q = f.search.toLowerCase();
      if (!person.name.toLowerCase().includes(q) &&
          !person.job_title.toLowerCase().includes(q)) return false;
    }
    // Exclude people with no capacity in the current period
    // (i.e. they haven't started yet or have already left)
    if (person.id?.startsWith("GENERIC-")) {
      const alloc = person.allocated?.[p] || 0;
      if (alloc <= 0) return false;
    } else {
      const cap = person.capacity?.[p];
      if (cap !== null && cap !== undefined && cap <= 0) return false;
    }
    return true;
}).sort((a, b) => {
    // Generics always at the bottom
    const aGeneric = a.department === "_GENERIC";
    const bGeneric = b.department === "_GENERIC";
    if (aGeneric && !bGeneric) return 1;
    if (!aGeneric && bGeneric) return -1;
    // Both generic — sort by job_title (P0, P1... P7, T0... T4, with Document Control last)
    if (aGeneric && bGeneric) {
      if (a.id === "GENERIC-UK-DOCUMENT-CONTROL") return 1;
      if (b.id === "GENERIC-UK-DOCUMENT-CONTROL") return -1;
      const gradeSort = t => {
        const m = t.match(/^([PT])(\d+)/);
        if (!m) return 999;
        const letter = m[1] === "P" ? 0 : 1;
        const num    = parseInt(m[2]);
        return letter * 100 + (99 - num);
      };
      return gradeSort(a.job_title || "") - gradeSort(b.job_title || "");
    }
    // Real staff — alphabetical
    return a.name.localeCompare(b.name);
  });
  // Apply user sort on top of default sort
  const periodGetters = Object.fromEntries(
    state.summary.periods.map(period => [`period_${period}`, ps => -(ps.allocated[period] || 0)])
  );
  return applySort(baseStaff, "staff", {
    name:      ps => ps.name,
    ...periodGetters,
  });
}

function filteredProjects() {
  const f = state.filters;
  const p = state.activePeriod;
  const base = state.summary.projects.filter(proj => {
    if (f.department !== "all" && proj.department !== f.department) return false;
    if (f.horizon !== "all" && proj.horizon_status !== f.horizon) return false;
    if (f.project_pm !== "all" && proj.pm !== f.project_pm) return false;
    if (f.project_pd !== "all" && proj.director !== f.project_pd) return false;
    // Exclude projects with no allocation in the current period
    if ((proj.total_days[p] || 0) === 0) return false;
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
  return applySort(base, "projects", {
    department: proj => proj.department,
    horizon:    proj => proj.horizon_status,
    pm:         proj => proj.pm,
    days:       proj => proj.total_days[p] || 0,
  });
}

// ---------------------------------------------------------------------------
// Render main view
// ---------------------------------------------------------------------------
// ── Management Summary ──────────────────────────────────────────────────────

function renderMgmtSummary() {
  const container = document.getElementById("mgmt-container");
  if (!container) return;
  const s = state.summary;
  if (!s) { container.innerHTML = '<div class="empty-state">Loading…</div>'; return; }

  const dept = state.filters.department !== "all" ? state.filters.department : null;
  const pd   = state.rtcFilters.pd || "";
  const pm   = state.rtcFilters.pm || "";
  const p    = state.activePeriod;
  const curIdx = Math.max(0, s.periods.indexOf(p));
  const periods6 = s.periods.slice(curIdx, curIdx + 6);
  const nextP = periods6[1] || periods6[0];

  const gradeSort = t => {
    if (!t) return 999;
    if (t.startsWith("L")) return -1;
    const m = t.match(/^([PT])(\d+)/);
    if (!m) return 998;
    return (m[1] === "P" ? 0 : 100) + (99 - parseInt(m[2]));
  };

  // Staff (no generics)
  const staff = s.staff.filter(ps =>
    !ps.id.startsWith("GENERIC-") && (!dept || ps.department === dept)
  );

  // RTCs
  const allRtcs = state.mgmtRtcs || state.rtcs;
  const rtcs = allRtcs.filter(r =>
    (!dept || r.department === dept) &&
    (!pd   || r.project_director === pd) &&
    (!pm   || r.project_manager  === pm)
  );

  // Horizon days by category
  const projsByDept = (s.projects || []).filter(pr => !dept || pr.department === dept);
  const linkedDays   = projsByDept.filter(pr => pr.horizon_status === "linked")
                         .reduce((sum, pr) => sum + (pr.future_days || 0), 0);
  const oppDays      = projsByDept.filter(pr => pr.horizon_status === "opportunity")
                         .reduce((sum, pr) => sum + (pr.future_days || 0), 0);
  const unlinkedDays = projsByDept.filter(pr => pr.horizon_status === "norecord")
                         .reduce((sum, pr) => sum + (pr.future_days || 0), 0);
  const linkedRtcs   = rtcs.filter(r => r.horizon_status === "linked");
  const oppRtcs      = rtcs.filter(r => r.horizon_status === "opportunity");
  const unlinkedRtcs = rtcs.filter(r => r.horizon_status === "norecord");

  // RTC review status
  const statusCounts = { current: 0, due_review: 0, overdue_review: 0 };
  rtcs.forEach(r => { if (r.status in statusCounts) statusCounts[r.status]++; });
  const totalRtcs = Object.values(statusCounts).reduce((s, v) => s + v, 0);

  // KPI numbers
  const overCount  = staff.filter(ps => ps.kpi[p] === "over").length;
  const underCount = staff.filter(ps => ps.kpi[p] === "under").length;
  const totalFte   = staff.reduce((sum, ps) => sum + (ps.fte[p] || 0), 0);
  const feeUtil    = (() => {
    const cap = staff.reduce((sum, ps) => sum + (ps.capacity[p] || 0), 0);
    const fee = staff.reduce((sum, ps) => sum + (ps.horizon_days[p] || 0), 0);
    return cap > 0 ? Math.round(fee / cap * 100) : 0;
  })();
  const bench = staff.reduce((sum, ps) => {
    const avail = (ps.capacity[p] || 0) - (ps.allocated[p] || 0);
    return sum + Math.max(0, avail);
  }, 0);

  // Top 10 projects
  const topProjects = projsByDept
    .filter(pr => (pr.future_days || 0) > 0)
    .sort((a, b) => (b.future_days || 0) - (a.future_days || 0))
    .slice(0, 10);

  // Over-allocated
  const overThisMonth = staff.filter(ps => ps.kpi[p] === "over")
    .sort((a, b) => (b.allocated[p] || 0) - (a.allocated[p] || 0));
  const overNextMonth = staff.filter(ps => ps.kpi[nextP] === "over")
    .sort((a, b) => (b.allocated[nextP] || 0) - (a.allocated[nextP] || 0));

  // Grade groups sorted L, P desc, T desc
  const gradeMap = {};
  staff.forEach(ps => {
    const g = ps.job_title || "Unknown";
    if (!gradeMap[g]) gradeMap[g] = { capacity: 0, allocated: 0, feeEarning: 0 };
    gradeMap[g].capacity  += ps.capacity[p] || 0;
    gradeMap[g].allocated += ps.allocated[p] || 0;
    gradeMap[g].feeEarning += ps.horizon_days[p] || 0;
  });
  const grades = Object.entries(gradeMap)
    .filter(([, g]) => g.capacity > 0)
    .sort(([a], [b]) => gradeSort(a) - gradeSort(b));

  // Annual leave expected vs actual (6 months)
  const AL_MONTHLY = { Jan:0.8,Feb:0.9,Mar:1.1,Apr:1.5,May:1.3,Jun:1.7,
                        Jul:2.4,Aug:3.6,Sep:2.3,Oct:1.9,Nov:1.5,Dec:6.0 };
  const AL_ANNUAL = 25;
  const alExpected = periods6.map(per => {
    const mon = per.split("-")[0];
    const bh = (s.bank_holidays || {})[per] || 0;
    return staff.reduce((sum, ps) => {
      const avail = ps.fte[per] || 1;
      return sum + ((AL_ANNUAL * avail) * ((AL_MONTHLY[mon] || 1) / 12)) + bh * avail;
    }, 0);
  });
  const alActual = periods6.map(per => {
    const alProjs = projsByDept.filter(pr => pr.number === "ID-06");
    return alProjs.reduce((sum, pr) => sum + (pr.total_days[per] || 0), 0);
  });

  // Allocated days by horizon type over 6 months
  const allocLinked   = periods6.map(per => projsByDept.filter(pr => pr.horizon_status === "linked").reduce((sum, pr) => sum + (pr.total_days[per] || 0), 0));
  const allocOpp      = periods6.map(per => projsByDept.filter(pr => pr.horizon_status === "opportunity").reduce((sum, pr) => sum + (pr.total_days[per] || 0), 0));
  const allocUnlinked = periods6.map(per => projsByDept.filter(pr => pr.horizon_status === "norecord").reduce((sum, pr) => sum + (pr.total_days[per] || 0), 0));

  // Treemap sizing
  const totalHdays = linkedDays + oppDays + unlinkedDays || 1;
  const linkedPct  = Math.round(linkedDays / totalHdays * 100);
  const oppPct     = Math.round(oppDays / totalHdays * 100);

  const fmtD = d => Math.round(d).toLocaleString("en-GB");

  const statusLabel  = { current:"Current", due_review:"Due for review", overdue_review:"Overdue review" };
  const statusColour = { current:"#008300", due_review:"#eda100", overdue_review:"#e34948" };

  const overTable = (people, per) => {
    if (!people.length) return `<div style="font-size:12px;color:var(--text-tertiary);font-style:italic;padding:8px 0">None</div>`;
    return `<table class="mgmt-table">
      <thead><tr><th>Name</th><th>Grade</th><th style="text-align:right">Allocated</th><th style="text-align:right">Capacity</th></tr></thead>
      <tbody>${people.map(ps => `<tr>
        <td>${escHtml(ps.name)}</td>
        <td style="font-size:11px;color:var(--text-tertiary)">${escHtml(fmt.gradeShort(ps.job_title))}</td>
        <td style="text-align:right;font-family:var(--font-mono);font-size:12px;color:#e34948">${fmtD(ps.allocated[per] || 0)}d</td>
        <td style="text-align:right;font-family:var(--font-mono);font-size:12px">${fmtD(ps.capacity[per] || 0)}d</td>
      </tr>`).join("")}</tbody></table>`;
  };

  container.innerHTML = `
    <div class="mgmt-grid">

      <!-- KPI row -->
      <div class="mgmt-card mgmt-card--wide">
        <div class="mgmt-card__title">This month at a glance — ${escHtml(p)}</div>
        <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px">
          ${[
            { val: staff.length,            label: "Staff",                  col: "" },
            { val: totalFte.toFixed(1),     label: "FTE",                   col: "" },
            { val: overCount,               label: "Over-allocated",         col: "#e34948" },
            { val: underCount,              label: "Under-resourced",        col: "#008300" },
            { val: feeUtil + "%",           label: "Fee-earning utilisation",col: "#2a78d6" },
            { val: fmtD(bench) + "d",       label: "Bench available",        col: "" },
          ].map(k => `<div style="background:var(--surface-1);border-radius:8px;padding:10px 12px">
            <div style="font-size:22px;font-weight:500;line-height:1;color:${k.col || "var(--text-primary)"}">
              ${escHtml(String(k.val))}
            </div>
            <div style="font-size:11px;color:var(--text-secondary);margin-top:3px">${escHtml(k.label)}</div>
          </div>`).join("")}
        </div>
      </div>

      <!-- Horizon treemap -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Horizon link status — future days</div>
        <div style="position:relative;height:130px">
          <div style="position:absolute;left:0;top:0;width:${linkedPct}%;height:100%;
                      background:#eaf3de;border:2px solid var(--surface-2);border-radius:4px;
                      display:flex;flex-direction:column;justify-content:center;align-items:center">
            <div style="font-size:11px;font-weight:500;color:#3b6d11">Fee earning</div>
            <div style="font-size:18px;font-weight:500;color:#3b6d11">${fmtD(linkedDays)}d</div>
            <div style="font-size:10px;color:#639922">${linkedRtcs.length} RTCs</div>
          </div>
          <div style="position:absolute;left:${linkedPct}%;top:0;width:${oppPct}%;height:55%;
                      background:#faeeda;border:2px solid var(--surface-2);border-radius:4px;
                      display:flex;flex-direction:column;justify-content:center;align-items:center">
            <div style="font-size:10px;font-weight:500;color:#854f0b">Opportunity</div>
            <div style="font-size:14px;font-weight:500;color:#854f0b">${fmtD(oppDays)}d</div>
            <div style="font-size:10px;color:#ba7517">${oppRtcs.length} RTCs</div>
          </div>
          <div style="position:absolute;left:${linkedPct}%;top:55%;width:${oppPct}%;height:45%;
                      background:#fcebeb;border:2px solid var(--surface-2);border-radius:4px;
                      display:flex;flex-direction:column;justify-content:center;align-items:center">
            <div style="font-size:10px;font-weight:500;color:#a32d2d">Not linked</div>
            <div style="font-size:12px;font-weight:500;color:#a32d2d">${fmtD(unlinkedDays)}d at risk</div>
            <div style="font-size:10px;color:#a32d2d">${unlinkedRtcs.length} RTCs</div>
          </div>
        </div>
      </div>

      <!-- RTC review status -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">RTC review status</div>
        ${Object.entries(statusCounts).map(([k, v]) => `
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <div style="font-size:11px;color:${statusColour[k]};width:120px;flex-shrink:0">${statusLabel[k]}</div>
            <div style="flex:1;height:8px;border-radius:4px;background:var(--surface-1);overflow:hidden">
              <div style="width:${totalRtcs ? Math.round(v/totalRtcs*100) : 0}%;height:100%;border-radius:4px;background:${statusColour[k]}"></div>
            </div>
            <div style="font-size:11px;color:var(--text-secondary);width:24px;text-align:right">${v}</div>
          </div>`).join("")}
      </div>

      <!-- Allocated days chart — full width -->
      <div class="mgmt-card mgmt-card--wide">
        <div class="mgmt-card__title">Allocated days — next 6 months</div>
        <div style="display:flex;align-items:flex-start;gap:16px">
          <div style="flex:1;position:relative;height:140px">
            <canvas id="mgmt-alloc-chart" role="img" aria-label="Stacked bar chart of allocated days by horizon status over 6 months">Allocated days by horizon status.</canvas>
          </div>
          <div style="display:flex;flex-direction:column;gap:8px;padding-top:4px;flex-shrink:0">
            ${[
              { col:"#2a78d6", label:"Fee earning" },
              { col:"#eda100", label:"Opportunity" },
              { col:"#e34948", label:"Not linked" },
            ].map(it => `<div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-secondary)">
              <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${it.col}"></span>${escHtml(it.label)}
            </div>`).join("")}
          </div>
        </div>
      </div>

      <!-- Annual leave -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Annual leave & public holidays</div>
        <div style="display:flex;align-items:flex-start;gap:16px">
          <div style="flex:1;position:relative;height:140px">
            <canvas id="mgmt-al-chart" role="img" aria-label="Expected vs actual annual leave and public holidays over 6 months">Annual leave expected vs actual.</canvas>
          </div>
          <div style="display:flex;flex-direction:column;gap:8px;padding-top:4px;flex-shrink:0">
            ${[
              { col:"#b4b2a9", label:"Expected" },
              { col:"#2a78d6", label:"Actual" },
            ].map(it => `<div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text-secondary)">
              <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${it.col}"></span>${escHtml(it.label)}
            </div>`).join("")}
          </div>
        </div>
      </div>

      <!-- Utilisation by grade -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Utilisation by grade — fee earning</div>
        <table class="mgmt-table">
          <thead><tr><th>Grade</th><th style="text-align:right">Capacity</th><th style="text-align:right">Fee earning</th><th style="text-align:right">Util.</th></tr></thead>
          <tbody>${grades.map(([grade, g]) => {
            const pct = g.capacity > 0 ? Math.round(g.feeEarning / g.capacity * 100) : 0;
            const col = pct >= 80 ? "#008300" : pct >= 50 ? "#eda100" : "#e34948";
            return `<tr>
              <td style="font-size:11px">${escHtml(grade)}</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px">${fmtD(g.capacity)}d</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px">${fmtD(g.feeEarning)}d</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px;color:${col};font-weight:500">${pct}%</td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>

      <!-- Bench strength by grade -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Bench strength by grade</div>
        <table class="mgmt-table">
          <thead><tr><th>Grade</th><th style="text-align:right">Capacity</th><th style="text-align:right">Allocated</th><th style="text-align:right">Available</th></tr></thead>
          <tbody>${grades.map(([grade, g]) => {
            const avail = Math.max(0, g.capacity - g.allocated);
            return `<tr>
              <td style="font-size:11px">${escHtml(grade)}</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px">${fmtD(g.capacity)}d</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px">${fmtD(g.allocated)}d</td>
              <td style="text-align:right;font-family:var(--font-mono);font-size:11px;color:#008300;font-weight:500">${fmtD(avail)}d</td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      </div>

      <!-- Over-allocated this month -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Over-allocated this month (${escHtml(p)})</div>
        ${overTable(overThisMonth, p)}
      </div>

      <!-- Over-allocated next month -->
      <div class="mgmt-card">
        <div class="mgmt-card__title">Over-allocated next month (${escHtml(nextP)})</div>
        ${overTable(overNextMonth, nextP)}
      </div>

      <!-- Top 10 projects — full width -->
      <div class="mgmt-card mgmt-card--wide">
        <div class="mgmt-card__title">Top 10 projects by days remaining</div>
        <table class="mgmt-table">
          <thead><tr><th>Project / task</th><th>Department</th><th style="text-align:right">This month</th><th style="text-align:right">Future days</th><th>Status</th></tr></thead>
          <tbody>${topProjects.map(pr => `<tr>
            <td>
              <div class="proj-name">${escHtml(pr.name)}</div>
              <div style="font-size:10px;color:var(--text-tertiary)">${escHtml(pr.task_name || "")}</div>
            </td>
            <td><span class="team-badge">${escHtml(pr.department || "—")}</span></td>
            <td style="text-align:right;font-family:var(--font-mono);font-size:12px">${fmtD(pr.total_days[p] || 0)}d</td>
            <td style="text-align:right;font-family:var(--font-mono);font-size:12px">${fmtD(pr.future_days || 0)}d</td>
            <td>${horizonBadge(pr.horizon_status)}</td>
          </tr>`).join("")}</tbody>
        </table>
      </div>

    </div>
  `;

  // Render Chart.js charts after DOM is set
  requestAnimationFrame(() => {
    const labels = periods6;

    const allocCtx = document.getElementById("mgmt-alloc-chart");
    if (allocCtx) {
      new Chart(allocCtx, {
        type: "bar",
        data: {
          labels,
          datasets: [
            { label: "Fee earning", data: allocLinked,   backgroundColor: "#2a78d6", stack: "a" },
            { label: "Opportunity", data: allocOpp,      backgroundColor: "#eda100", stack: "a" },
            { label: "Not linked",  data: allocUnlinked, backgroundColor: "#e34948", stack: "a" },
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { stacked: true, grid: { display: false }, ticks: { font: { size: 10 } } },
            y: { stacked: true, grid: { color: "#e1e0d9" }, ticks: { font: { size: 10 }, callback: v => Math.round(v).toLocaleString("en-GB") } }
          }
        }
      });
    }

    const alCtx = document.getElementById("mgmt-al-chart");
    if (alCtx) {
      new Chart(alCtx, {
        type: "bar",
        data: {
          labels,
          datasets: [
            { label: "Expected", data: alExpected.map(v => Math.round(v)), backgroundColor: "#b4b2a9" },
            { label: "Actual",   data: alActual.map(v => Math.round(v)),   backgroundColor: "#2a78d6" },
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 10 } } },
            y: { grid: { color: "#e1e0d9" }, ticks: { font: { size: 10 } } }
          }
        }
      });
    }
  });
}

function renderView() {
  const panels = ["staff-panel", "projects-panel", "rtcs-panel", "mgmt-panel"];
  panels.forEach(id => document.getElementById(id)?.classList.add("hidden"));

  if (state.activeView === "staff") {
    renderStaffTable();
    document.getElementById("staff-panel").classList.remove("hidden");
  } else if (state.activeView === "mgmt") {
    document.getElementById("detail-panel").style.display = "none";
    fetch("/api/rtcs")
      .then(r => r.json())
      .then(data => {
        state.mgmtRtcs = Array.isArray(data) ? data : (data.rtcs || []);
        renderMgmtSummary();
      })
      .catch(() => renderMgmtSummary());
    document.getElementById("mgmt-panel").classList.remove("hidden");
  } else {
    // Default: projects view (merged RTCs + Projects)
    document.getElementById("detail-panel").style.display = "";
    renderProjectTable();
    document.getElementById("projects-panel").classList.remove("hidden");
  }
  renderMetrics();
}

// ---------------------------------------------------------------------------
// Staff table
// ---------------------------------------------------------------------------
// Tracks which staff rows are expanded
const _expandedStaff = new Set();
const _expandedRtcs  = new Set();

function kpiDot(kpi) {
  const col = kpi === "over"  ? "#DC2626"
            : kpi === "under" ? "#15803D"
            : "#D97706";
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                       background:${col};margin-left:5px;vertical-align:middle;
                       flex-shrink:0"></span>`;
}

function renderStaffTable() {
  const tbody = document.getElementById("staff-tbody");
  const staff = filteredStaff();
  const s     = state.summary;

  // Build 6 consecutive periods from current month
  const curIdx = Math.max(0, s.periods.indexOf(state.activePeriod));
  const cols   = s.periods.slice(curIdx, curIdx + 6);

  // Update column headers dynamically
  const thead = document.querySelector(".staff-table thead tr");
  if (thead) {
    thead.innerHTML =
      `<th class="sortable" onclick="toggleSort('staff','name')" style="min-width:180px">
         Name / Title / Function <span id="sort-staff-name"></span>
       </th>` +
      cols.map(p => `<th style="text-align:center;min-width:72px;white-space:nowrap;cursor:pointer"
                         onclick="toggleSort('staff','period_${p}')">${escHtml(p)}
                         <span id="sort-staff-period_${p.replace(/-/g,'_')}"></span></th>`).join("") ;
  }

  if (staff.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length + 1}">
      <div class="empty-state">No staff match the current filters</div>
    </td></tr>`;
    return;
  }

  // Build project lookup keyed by project_id
  const projLookup = Object.fromEntries(
    (s.projects || []).map(pr => [pr.project_id, pr])
  );

  const rows = [];

  staff.forEach(person => {
    const isGeneric  = person.id?.startsWith("GENERIC-");
    const isExpanded = _expandedStaff.has(person.id);

    // Person row
    const dayCells = cols.map(p => {
      const alloc = person.allocated[p] || 0;
      const kpi   = person.kpi[p];
      if (alloc === 0) return `<td></td>`;
      return `<td style="text-align:center;font-family:var(--font-mono);font-size:12px;white-space:nowrap;vertical-align:middle">
        <span>${alloc.toFixed(2)}d</span>${isGeneric ? "" : kpiDot(kpi)}
      </td>`;
    }).join("");

    rows.push(`<tr data-id="${escHtml(person.id)}"
                   class="staff-person-row${isExpanded ? " staff-row--expanded" : ""}"
                   style="cursor:pointer">
      <td>
        <div class="staff-name" style="font-weight:600">${escHtml(person.name)}</div>
        <div class="staff-grade">${escHtml(person.job_title || "")}</div>
        <div class="staff-grade" style="color:var(--text-tertiary)">${escHtml(person.job_function || "")}</div>
      </td>
      ${dayCells}
    </tr>`);

    // Expanded project rows
    if (isExpanded) {
      const projRows = (person.projects || [])
        .filter(pr => cols.some(p => (pr.days[p] || 0) > 0))
        .sort((a, b) => {
          const aTotal = cols.reduce((s, p) => s + (a.days[p] || 0), 0);
          const bTotal = cols.reduce((s, p) => s + (b.days[p] || 0), 0);
          return bTotal - aTotal;
        });

      projRows.forEach(pr => {
        const proj = projLookup[pr.project_id];
        const name = proj ? (proj.name || proj.task_name || `Project ${pr.project_id}`) : `Project ${pr.project_id}`;
        const task = proj ? (proj.task_name || "") : "";

        const projDayCells = cols.map(p => {
          const d = pr.days[p] || 0;
          if (d === 0) return `<td></td>`;
          return `<td style="text-align:center;font-family:var(--font-mono);font-size:11px;
                              color:var(--text-secondary);white-space:nowrap">
            ${d.toFixed(2)}d
          </td>`;
        }).join("");

        rows.push(`<tr class="staff-project-row" style="background:var(--surface-2)">
          <td style="padding-left:24px">
            <div style="font-size:11px;color:var(--text-secondary)">${escHtml(name)}</div>
            ${task ? `<div style="font-size:10px;color:var(--text-tertiary)">${escHtml(task)}</div>` : ""}
          </td>
          ${projDayCells}
        </tr>`);
      });
    }
  });

  tbody.innerHTML = rows.join("");

  // Expand/collapse on click
  tbody.querySelectorAll("tr.staff-person-row").forEach(row => {
    row.addEventListener("click", () => {
      const id = row.dataset.id;
      if (_expandedStaff.has(id)) {
        _expandedStaff.delete(id);
      } else {
        _expandedStaff.add(id);
      }
      renderStaffTable();
    });
  });

  updateSortIndicators("staff", ["name", ...cols.map(p => `period_${p}`)]);
}

// ---------------------------------------------------------------------------
// Project table
// ---------------------------------------------------------------------------
function statusBadge(status) {
  const map = {
    current:            ["rtc-badge rtc-badge--current",   "Current"],
    due_review:         ["rtc-badge rtc-badge--review",    "Due for review"],
    overdue_review:     ["rtc-badge rtc-badge--overdue",   "Overdue review"],
    awaiting_archiving: ["rtc-badge rtc-badge--archiving", "Awaiting archiving"],
    archived:           ["rtc-badge rtc-badge--archived",  "Archived"],
  };
  const [cls, label] = map[status] || ["rtc-badge", status];
  return `<span class="${cls}">${label}</span>`;
}

function renderProjectTable() {
  const tbody = document.getElementById("project-tbody");
  const rtcs  = filteredRtcs();
  const p     = state.activePeriod;
  const s     = state.summary;

  if (rtcs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6">
      <div class="empty-state">No RTCs match the current filters</div>
    </td></tr>`;
    return;
  }

  const rows = [];

  rtcs.forEach(r => {
    const isExpanded = _expandedRtcs.has(r.rtc_id);

    rows.push(`<tr data-id="${r.rtc_id}"
                   class="proj-person-row${isExpanded ? " proj-row--expanded" : ""}"
                   style="cursor:pointer">
      <td>
        ${r.is_placeholder_number ? "" :
          `<div class="proj-number">${escHtml(r.display_project_number || r.project_number || "")}</div>
           <div class="proj-task">${escHtml(r.display_task_order || r.task_order_number || "")}</div>`}
      </td>
      <td>
        ${r.project_customer ? `<div class="proj-customer">${escHtml(r.project_customer)}</div>` : ""}
        <div class="proj-name">${escHtml(r.project_name || "No project name")}</div>
        ${r.task_name ? `<div class="proj-task">${escHtml(r.task_name)}</div>` : ""}
      </td>
      <td><span class="team-badge">${escHtml(r.department || "—")}</span></td>
      <td>
        <div>${horizonBadge(r.horizon_status)}</div>
        <div style="margin-top:3px">${statusBadge(r.status)}</div>
      </td>
      <td style="text-align:right;font-family:var(--font-mono);font-size:12px">
        ${fmt.days(r.current_month_days || 0)}d
      </td>
      <td style="text-align:right;font-family:var(--font-mono);font-size:12px">
        ${fmt.days(r.future_days || 0)}d
      </td>
    </tr>`);

    if (isExpanded) {
      // Build staff list for this period from summary
      const curIdx = Math.max(0, s.periods.indexOf(state.activePeriod));
      const cols   = s.periods.slice(curIdx, curIdx + 6);
      const rtcMatch = state.rtcs.find(rx => rx.rtc_id === r.rtc_id);

      const staffList = (s.staff || [])
        .map(person => {
          const proj = (person.projects || []).find(pr =>
            rtcMatch && pr.project_id === rtcMatch.project_id
          );
          const totalDays = cols.reduce((sum, col) => sum + (proj?.days[col] || 0), 0);
          const daysByPeriod = cols.map(col => proj?.days[col] || 0);
          return { name: person.name, job_title: person.job_title, daysByPeriod, totalDays };
        })
        .filter(ps => ps.totalDays > 0)
        .sort((a, b) => {
          const gradeSort = t => {
            const m = (t || '').match(/^([PT])(\d+)/);
            if (!m) return 999;
            return (m[1] === 'P' ? 0 : 1) * 100 + (99 - parseInt(m[2]));
          };
          const ga = gradeSort(a.job_title);
          const gb = gradeSort(b.job_title);
          if (ga !== gb) return ga - gb;
          return (a.name || '').localeCompare(b.name || '');
        });

      const colHeaders = cols.map(col =>
        `<th style="text-align:right;font-size:10px;color:var(--text-tertiary);
                    white-space:nowrap;padding:0 6px;font-weight:500">${escHtml(col)}</th>`
      ).join("");

      const staffRows = staffList.length
        ? `<table style="font-size:11px;border-collapse:collapse;table-layout:fixed;width:100%">
             <colgroup>
               <col style="width:280px">
               ${cols.map(() => `<col style="width:68px">`).join("")}
             </colgroup>
             <thead><tr>
               <th style="text-align:left;font-size:10px;color:var(--text-tertiary);
                          font-weight:500;padding-bottom:4px">Name</th>
               ${colHeaders}
             </tr></thead>
             <tbody>
               ${staffList.map(ps =>
                 `<tr>
                    <td style="color:var(--text-secondary);padding:2px 0">${escHtml(ps.name)}</td>
                    ${ps.daysByPeriod.map(d =>
                      `<td style="text-align:right;font-family:var(--font-mono);
                                  padding:2px 6px;color:${d > 0 ? 'var(--text-primary)' : 'var(--text-tertiary)'}"
                          >${d > 0 ? d.toFixed(2) + 'd' : ''}</td>`
                    ).join("")}
                  </tr>`
               ).join("")}
             </tbody>
           </table>`
        : `<div style="font-size:11px;color:var(--text-tertiary);font-style:italic">No allocations in this period</div>`;

      const fmtDate = iso => iso
        ? new Date(iso).toLocaleDateString("en-GB", { day:"numeric", month:"short", year:"numeric" })
        : "Never";

      rows.push(`<tr class="proj-project-row" style="background:var(--surface-2)">
        <td colspan="6" style="padding:12px 16px">
          <div style="display:flex;gap:32px;flex-wrap:wrap">
            <div style="min-width:220px">
              <div style="font-size:11px;font-weight:600;text-transform:uppercase;
                          letter-spacing:0.06em;color:var(--text-tertiary);margin-bottom:6px">Details</div>
              <div style="font-size:12px;line-height:1.8;color:var(--text-secondary)">
                <div><strong>Customer:</strong> ${escHtml(r.project_customer || "—")}</div>
                <div><strong>Project Director:</strong> ${escHtml(r.project_director || "—")}</div>
                <div><strong>Project Manager:</strong> ${escHtml(r.project_manager || "—")}</div>
                <div><strong>Last opened:</strong> ${fmtDate(r.last_opened)}</div>
                <div><strong>Last opened by:</strong> ${escHtml(r.last_opened_by || "—")}</div>
              </div>
              <div style="margin-top:10px">
                <a href="/rtc/${r.rtc_id}" class="btn-open-rtc">Open to edit →</a>
              </div>
            </div>
            <div style="min-width:200px;flex:1">
              <div style="font-size:11px;font-weight:600;text-transform:uppercase;
                          letter-spacing:0.06em;color:var(--text-tertiary);margin-bottom:6px">
                Six Month Look Ahead
              </div>
              ${staffRows}
            </div>
          </div>
        </td>
      </tr>`);
    }
  });

  tbody.innerHTML = rows.join("");

  tbody.querySelectorAll("tr.proj-person-row").forEach(row => {
    row.addEventListener("click", () => {
      const id = parseInt(row.dataset.id);
      if (_expandedRtcs.has(id)) {
        _expandedRtcs.delete(id);
      } else {
        _expandedRtcs.add(id);
      }
      renderProjectTable();
    });
  });
  updateSortIndicators("projects", ["department","horizon","status","this_month","future_days"]);
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
  if (state.activePeriod)   params.set("period", state.activePeriod);

  try {
    const r = await fetch(`/api/rtcs?${params}`);
    state.rtcs = await r.json();
    buildFilterOptions();
    renderProjectTable();
    renderMetrics();
  } catch(e) {
    console.error("Failed to load RTCs:", e);
  }
}

function filteredRtcs() {
  const statusFilter = state.rtcFilters.status;
  const horizonFilter = state.filters.horizon !== "all" ? state.filters.horizon : "";
  const p = state.activePeriod;
  let base = statusFilter
    ? state.rtcs.filter(r => r.status === statusFilter)
    : state.rtcs;
  if (horizonFilter) base = base.filter(r => r.horizon_status === horizonFilter);
  return applySort(base, "projects", {
    department:   r => r.department,
    pd:           r => r.project_director,
    pm:           r => r.project_manager,
    status:       r => ({
      current: 0, due_review: 1, overdue_review: 2,
      awaiting_archiving: 3, archived: 4
    })[r.status] ?? 9,
    horizon:      r => r.horizon_status,
    this_month:   r => (r.current_month_days || 0),
    future_days:  r => (r.future_days || 0),
    last_updated: r => r.last_updated_at,
  });
}

function horizonBadge(hs) {
    const map = {
      linked:      ["horizon horizon--linked",      "Linked to Horizon"],
      opportunity: ["horizon horizon--opportunity",  "Opportunity"],
      other:       ["horizon horizon--other",        "Other record"],
      norecord:    ["horizon horizon--norecord",     "Not linked"],
    };
    const [cls, label] = map[hs] || ["horizon horizon--norecord", "Not linked"];
    return `<span class="${cls}"><span class="horizon--dot"></span>${label}</span>`;
  }

function selectRtc(id) {
  const rtc = state.rtcs.find(r => r.rtc_id === id);
  if (!rtc) return;

  state.selectedRtc     = id;
  state.selectedStaff   = null;
  state.selectedProject = null;

  renderProjectTable();
  showRtcDetail(rtc);
}

function showRtcDetail(rtc) {
  const panel = document.getElementById("detail-panel");

  const avatarMap = {
    current:            "✓",
    due_review:         "!",
    overdue_review:     "!",
    awaiting_archiving: "⌛",
    archived:           "✗",
  };
  document.getElementById("dp-avatar").textContent = avatarMap[rtc.status] || "!";

  const avatarColourMap = {
    current:            "var(--green-dark)",
    due_review:         "var(--amber-dark)",
    overdue_review:     "var(--status-red)",
    awaiting_archiving: "var(--text-tertiary)",
    archived:           "var(--text-tertiary)",
  };
  document.getElementById("dp-avatar").style.color = avatarColourMap[rtc.status] || "";

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
  const lblsR = document.querySelectorAll(".detail-stat__label");
  if (lblsR[0]) lblsR[0].textContent = "This month";
  if (lblsR[1]) lblsR[1].textContent = "Future days";
  if (lblsR[2]) lblsR[2].textContent = "Last opened";

  // Hide KPI badge and no-record warning (not applicable for RTCs)
  const kpiEl = document.getElementById("dp-kpi");
  if (kpiEl) { kpiEl.className = "kpi kpi--ok"; kpiEl.textContent = ""; }
  document.getElementById("dp-norec-warn")?.classList.add("hidden");


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
    <div><strong>Customer</strong>: ${escHtml(rtc.project_customer || "-")}</div>
    <div><strong>Project name</strong>: ${escHtml(rtc.project_name || "-")}</div>
    <div><strong>Task name</strong>: ${escHtml(rtc.task_name || "-")}</div>
    <div><strong>Project Director</strong>: ${escHtml(rtc.project_director || "-")}</div>
    <div><strong>Project Manager</strong>: ${escHtml(rtc.project_manager || "-")}</div>
    <div style="margin-top:8px"><strong>Last opened by</strong>: ${escHtml(rtc.last_opened_by || "-")}</div>
    <div style="margin-top:8px">${horizonBadge(rtc.horizon_status)}</div>
    <div style="margin-top:3px">${statusBadge(rtc.status)}</div>
      <div style="margin-top:10px;font-size:10px;font-weight:600;text-transform:uppercase;
                  letter-spacing:0.08em;color:var(--text-tertiary);margin-bottom:4px">
        Staff this period
      </div>
      ${(() => {
        const p = state.activePeriod;
        const allocated = (state.summary.staff || [])
          .filter(s => s.projects.some(pr => {
            const rtcMatch = state.rtcs.find(r => r.rtc_id === rtc.rtc_id);
            return rtcMatch && pr.project_id === rtcMatch.project_id && (pr.days[p] || 0) > 0;
          }))
          .map(s => ({
            name: s.name,
            job_title: s.job_title,
            days: (s.projects.find(pr => {
              const rtcMatch = state.rtcs.find(r => r.rtc_id === rtc.rtc_id);
              return rtcMatch && pr.project_id === rtcMatch.project_id;
            })?.days[p] || 0)
          }))
          .filter(s => s.days > 0)
          .sort((a, b) => b.days - a.days);
        if (!allocated.length) return '<div style="font-size:11px;color:var(--text-tertiary)">No allocations this period</div>';
        return allocated.map(s => `
          <div class="detail-project-row">
            <span class="team-badge">${fmt.gradeShort(s.job_title)}</span>
            <span class="detail-proj-name">${escHtml(s.name)}</span>
            <span class="detail-proj-days">${fmt.days(s.days)}d</span>
          </div>`).join('');
      })()}
      <div style="margin-top:12px">
        <a href="/rtc/${rtc.rtc_id}" class="btn-open-rtc">Open to edit →</a>
      </div>
    </div>`;

  // Check for linkable Horizon record

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
  // Set stat labels for People view
  const lbls = document.querySelectorAll(".detail-stat__label");
  if (lbls[0]) lbls[0].textContent = "Allocated";
  if (lbls[1]) lbls[1].textContent = "Capacity";
  if (lbls[2]) lbls[2].textContent = "Remaining";
  // Hide RTC-specific elements
  // Hide RTC/project-specific elements
  document.getElementById("dp-norec-warn")?.classList.add("hidden");
  const staffKpiEl = document.getElementById("dp-kpi");
  if (staffKpiEl) { staffKpiEl.className = ""; staffKpiEl.innerHTML = ""; }
  document.getElementById("dp-stat-remain").style.color   =
    (cap - alloc) < 0 ? "var(--red)" : "var(--green-dark)";

  // KPI badge
  document.getElementById("dp-kpi").className = `kpi kpi--${kpi}`;
  document.getElementById("dp-kpi").textContent = kpi;

  // Projects breakdown
  const projContainer = document.getElementById("dp-projects");
  const projectLookup = Object.fromEntries(
    state.summary.projects.map(pr => [pr.project_id, pr])
  );

  const rows = person.projects
    .filter(pr => (pr.days[p] || 0) > 0)
    .sort((a, b) => (b.days[p] || 0) - (a.days[p] || 0));

  // Count unlinked projects for this person this period
  const unlinkedCount = rows.filter(pr => {
    const proj = projectLookup[pr.project_id];
    return proj && proj.horizon_status === "norecord";
  }).length;

  const warnEl = document.getElementById("dp-norec-warn");
  if (unlinkedCount > 0) {
    warnEl.classList.remove("hidden");
    document.getElementById("dp-norec-days").textContent =
      `${unlinkedCount} of these project${unlinkedCount !== 1 ? "s are" : " is"} not linked to Horizon and will not generate revenue`;
  } else {
    warnEl.classList.add("hidden");
  }

  if (rows.length === 0) {
    projContainer.innerHTML =
      `<div class="empty-state" style="padding:16px">No allocations this period</div>`;
  } else {
    projContainer.innerHTML = rows.map(pr => {
      const proj   = projectLookup[pr.project_id];
      const name   = proj ? proj.name : `Project ${pr.project_id}`;
      const days   = pr.days[p] || 0;
      return `<div class="detail-project-row">
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
  const panel  = document.getElementById("detail-panel");
  const p      = state.activePeriod;
  const linked = proj.horizon_status === "linked";

  // Avatar — show horizon status icon instead of project number characters
  document.getElementById("dp-avatar").textContent = linked ? "✓" : "!";

  document.getElementById("dp-name").textContent = proj.name;
  document.getElementById("dp-role").textContent =
    [proj.task_name, proj.department].filter(Boolean).join(" · ");

  // This period days
  const thisPeriodDays = proj.total_days[p] || 0;

  // Future days — from server-side summary (fixed from current month)
  const futureDays = proj.future_days || 0;

  document.getElementById("dp-stat-alloc").textContent = fmt.days(thisPeriodDays) + "d";
  document.getElementById("dp-stat-cap").textContent   = fmt.days(futureDays) + "d";
  document.getElementById("dp-stat-remain").textContent = proj.pm || "—";
  document.getElementById("dp-stat-remain").style.color = "";

  const hs = proj.horizon_status || (linked ? "linked" : "norecord");
  const hsMap = {
    linked: ["linked", "Linked to Horizon"],
    opportunity: ["opportunity", "Opportunity"],
    other: ["other", "Other record"],
    norecord: ["norecord", "Not linked to Horizon"],
  };
  const [hsCls, hsLabel] = hsMap[hs] || ["norecord", "Not linked"];
  document.getElementById("dp-kpi").className = `horizon horizon--${hsCls}`;
  document.getElementById("dp-kpi").innerHTML =
    `<span class="horizon--dot"></span>${hsLabel}`;

  // No-record warning
  const warnEl = document.getElementById("dp-norec-warn");
  if (!linked) {
    warnEl.classList.remove("hidden");
    document.getElementById("dp-norec-days").textContent =
      "This project has no Horizon record. Time allocated to it will not generate revenue.";
  } else {
    warnEl.classList.add("hidden");
  }

  // Stat labels
  const labels = document.querySelectorAll(".detail-stat__label");
  if (labels[0]) labels[0].textContent = "This period";
  if (labels[1]) labels[1].textContent = "Future days";
  if (labels[2]) labels[2].textContent = "Project Manager";

  // Staff allocated this period
  const projContainer = document.getElementById("dp-projects");
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
    projContainer.innerHTML = `
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
  } else {
    projContainer.innerHTML = "";
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
  if (state.activeView === "projects") {
    loadRtcs();
  } else {
    renderView();
    renderMetrics();
  }
  if (state.selectedStaff) {
    const person = state.summary.staff.find(p => String(p.id) === String(state.selectedStaff));
    if (person) showStaffDetail(person);
  }
  if (state.selectedRtc) {
    const rtc = state.rtcs.find(r => r.rtc_id === state.selectedRtc);
    if (rtc) showRtcDetail(rtc);
  }
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function switchView(view) {
  state.activeView = view;
  history.replaceState(null, "", "#" + view);
  state.selectedStaff   = null;
  state.selectedProject = null;
  state.selectedRtc     = null;
  closeDetailPanel();

  // Clear expanded staff rows when switching views
  if (view !== "staff")    _expandedStaff.clear();
  if (view !== "projects") _expandedRtcs.clear();

  // Reset filters when switching views
  document.querySelectorAll(".filter-bar select").forEach(sel => {
    sel.value = sel.options[0]?.value ?? "all";
  });
  const lmSel = document.getElementById("filter-line-manager");
  if (lmSel) lmSel.value = "all";
  state.filters.horizon      = "all";
  state.filters.job_title    = "all";
  state.filters.job_function = "all";
  state.filters.line_manager = "all";
  state.filters.department   = "all";
  state.rtcFilters.pd        = "";
  state.rtcFilters.pm        = "";
  state.rtcFilters.status    = "";

  document.querySelectorAll(".topnav__tab[data-view]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.view === view);
  });

  // Show/hide filter slots per view
  const filterSlots = {
    projects: ["filter-rtc-pd", "filter-rtc-pm", "filter-rtc-status", "filter-horizon"],
    staff: ["filter-job-title", "filter-job-function", "filter-line-manager", "filter-slot4-spacer"],
    mgmt:     ["filter-rtc-pd", "filter-rtc-pm", "filter-horizon"],
  };
  const hiddenSlots = {
    staff:    ["filter-slot4-spacer"],
    projects: [],
    mgmt:     [],
  };
  const allSlots = [
    "filter-rtc-pd", "filter-rtc-pm", "filter-rtc-status",
    "filter-job-title", "filter-job-function", "filter-line-manager",
    "filter-horizon", "filter-project-pd", "filter-project-pm",
    "filter-slot4-spacer",
  ];
  allSlots.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = "none";
  });
  (filterSlots[view] || []).forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.style.display = ""; el.style.visibility = "visible"; }
  });
  (hiddenSlots[view] || []).forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.style.display = ""; el.style.visibility = "hidden"; }
  });

  // Month tabs only relevant for staff and projects views
 const monthTabs = document.getElementById("month-tabs");
  if (monthTabs) monthTabs.style.display = (view === "mgmt" || view === "staff" || view === "projects") ? "none" : "";
  const newRtcBtn = document.getElementById("btn-create-rtc");
  if (newRtcBtn) newRtcBtn.style.display = view === "projects" ? "" : "none";
  const detailPanel = document.getElementById("detail-panel");
  if (detailPanel && view === "mgmt") detailPanel.classList.remove("open");

  // Reload data fresh on every tab switch so changes made on one tab
  // are reflected immediately on the others without needing a page refresh
  if (view === "projects") {
    loadRtcs();
  } else {
    loadSummary().then(() => renderView());
    return;
  }

  renderView();
}

// ---------------------------------------------------------------------------
// Close detail panel
// ---------------------------------------------------------------------------
function closeDetailPanel() {
  const panel = document.getElementById("detail-panel");
  panel.classList.remove("open");
  panel.style.display = state.activeView === "mgmt" ? "none" : "";
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
    el.innerHTML = `<strong>${s.staff.length}</strong> staff · ` +
                   `<strong>${s.projects.length}</strong> projects`;
  }
}

// ---------------------------------------------------------------------------
// Reset all filters back to defaults
// ---------------------------------------------------------------------------
function resetFilters() {
  state.sort.staff    = { col: null, dir: "asc" };
  state.sort.projects = { col: "this_month", dir: "desc" };
  _expandedRtcs.clear();
  _expandedStaff.clear();
  state.filters.job_function = "all";
  state.filters.job_title    = "all";
  state.filters.department   = "all";
  state.filters.horizon      = "all";
  state.filters.project_pm   = "all";
  state.filters.project_pd   = "all";
  state.filters.search       = "";
  state.filters.line_manager = "all";
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
  
  const projectPmSel = document.getElementById("filter-project-pm");
  if (projectPmSel) projectPmSel.value = "all";

  const projectPdSel = document.getElementById("filter-project-pd");
  if (projectPdSel) projectPdSel.value = "all";

  if (searchEl)   searchEl.value   = "";
  if (pmSel)      pmSel.value      = "all";
  if (pdSel)      pdSel.value      = "all";
  if (statusSel)  statusSel.value  = "";

  state.filters.line_manager = "all";
  const lmSel = document.getElementById("filter-line-manager");
  if (lmSel) lmSel.value = "all";

  if (["rtcs","projects"].includes(state.activeView)) {
    loadRtcs();
  } else {
    renderView();
  }
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
  if (document.getElementById("rtc-customer"))
    document.getElementById("rtc-customer").value = "";
  document.getElementById("rtc-start-date").value  = "";
  document.getElementById("rtc-department").value  = "";
  document.getElementById("rtc-pd").value           = "";
  document.getElementById("rtc-pm").value           = "";
  document.getElementById("rtc-pd-external").checked = false;
  document.getElementById("rtc-pm-external").checked = false;
  clearProjectLookup();
  document.getElementById("rtc-form-error").textContent = "";
  document.getElementById("rtc-form-error").classList.add("hidden");

  const deptSel = document.getElementById("rtc-department");
  deptSel.innerHTML = "<option value=\"\">Select department\u2026</option>";
  (state.summary?.departments || []).forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.department; opt.textContent = d.department;
    deptSel.appendChild(opt);
  });

  // Clear PD/PM until a department is chosen
  populatePersonDropdown("rtc-pd", null);
  populatePersonDropdown("rtc-pm", null);

  const now = new Date();
  _rtcPickerYear  = now.getFullYear();
  _rtcPickerMonth = now.getMonth() + 1;
  document.getElementById("rtc-start-date").value =
    `${_rtcPickerYear}-${String(_rtcPickerMonth).padStart(2,"0")}-01`;
  renderMonthPicker();

  // Wire department change -> repopulate PD/PM
  document.getElementById("rtc-department").onchange = () => {
    const dept       = document.getElementById("rtc-department").value;
    const pdExternal = document.getElementById("rtc-pd-external").checked;
    const pmExternal = document.getElementById("rtc-pm-external").checked;
    populatePersonDropdown("rtc-pd", pdExternal ? null : dept);
    populatePersonDropdown("rtc-pm", pmExternal ? null : dept);
  };

  // Wire PD external checkbox
  document.getElementById("rtc-pd-external").onchange = () => {
    const dept       = document.getElementById("rtc-department").value;
    const pdExternal = document.getElementById("rtc-pd-external").checked;
    populatePersonDropdown("rtc-pd", pdExternal ? null : dept);
  };

  // Wire PM external checkbox
  document.getElementById("rtc-pm-external").onchange = () => {
    const dept       = document.getElementById("rtc-department").value;
    const pmExternal = document.getElementById("rtc-pm-external").checked;
    populatePersonDropdown("rtc-pm", pmExternal ? null : dept);
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

  // Real staff — filtered by department if specified, generics excluded
  const real = staff.filter(s => s.department !== "_GENERIC" &&
    (!department || s.department === department));

  [...real].sort((a, b) => (a.name || "").localeCompare(b.name || "")).forEach(s => {
    const opt = document.createElement("option");
    opt.value = s.name; opt.textContent = s.name;
    sel.appendChild(opt);
  });

  // Generic placeholders — always at the bottom in grade order,
  // with Document Control explicitly last
  const generics = staff
    .filter(s => s.department === "_GENERIC")
    .sort((a, b) => {
      if (a.id === "GENERIC-UK-DOCUMENT-CONTROL") return 1;
      if (b.id === "GENERIC-UK-DOCUMENT-CONTROL") return -1;
      const gradeSort = t => {
        const m = t.match(/^([PT])(\d+)/);
        if (!m) return 999;
        const letter = m[1] === "P" ? 0 : 1;
        const num    = parseInt(m[2]);
        return letter * 100 + (99 - num);
      };
      return gradeSort(a.job_title || "") - gradeSort(b.job_title || "");
    });

  if (generics.length) {
    const divider = document.createElement("option");
    divider.disabled = true;
    divider.textContent = "\u2500\u2500 Generic roles \u2500\u2500";
    sel.appendChild(divider);
    generics.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.name;
      opt.style.fontStyle = "italic";
      sel.appendChild(opt);
    });
  }
}

function closeRtcModal() {
  document.getElementById("rtc-modal-overlay").classList.add("hidden");
}

function renderMonthPicker() {
  document.getElementById("rtc-year-label").textContent = _rtcPickerYear;
  const grid = document.getElementById("rtc-month-grid");
  if (!grid) return;
  const now         = new Date();
  const currentYear = now.getFullYear();
  const currentMonth = now.getMonth() + 1;
  grid.innerHTML = MONTH_ABBR.map((name, i) => {
    const month   = i + 1;
    const isPast  = _rtcPickerYear < currentYear ||
                    (_rtcPickerYear === currentYear && month < currentMonth);
    const sel     = month === _rtcPickerMonth ? "selected" : "";
    const disabled = isPast ? "disabled" : "";
    return `<button type="button" class="month-picker__btn ${sel}" data-month="${month}" ${disabled}>${name}</button>`;
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
    const resultEl      = document.getElementById("rtc-lookup-result");
    const placeholderEl = document.getElementById("rtc-lookup-placeholder");
    const manualFields  = document.getElementById("rtc-manual-fields");

    if (d.match_type === "full") {
      // Exact match — auto-fill everything, no manual fields needed
      document.getElementById("rtc-lookup-name").textContent = d.project_name     || "\u2014";
      document.getElementById("rtc-lookup-task").textContent = d.task_name        || "\u2014";
      document.getElementById("rtc-lookup-pm").textContent   = d.project_manager  || "\u2014";
      document.getElementById("rtc-lookup-pd").textContent   = d.project_director || "\u2014";
      resultEl.classList.remove("hidden");
      placeholderEl.classList.add("hidden");
      manualFields.classList.add("hidden");
      // Auto-fill department if it matches a known department
      _autoFillDepartment(d.project_organisation);
      _preselectPerson("rtc-pd", d.project_director);
      _preselectPerson("rtc-pm", d.project_manager);

    } else if (d.match_type === "project_only") {
      // Project known, task order new — auto-fill project-level fields,
      // but show task name as the only required manual entry
      document.getElementById("rtc-lookup-name").textContent = d.project_name     || "\u2014";
      document.getElementById("rtc-lookup-task").textContent = "New task \u2014 enter name below";
      document.getElementById("rtc-lookup-pm").textContent   = d.project_manager  || "\u2014";
      document.getElementById("rtc-lookup-pd").textContent   = d.project_director || "\u2014";
      resultEl.classList.remove("hidden");

      // Show a more specific message
      placeholderEl.innerHTML = "<strong>Project found in Horizon \u2014 task order not yet available.</strong> "
        + "The task name below will be updated automatically when this task order appears in the next PAR import. "
        + "All other details have been auto-filled and can be adjusted if needed.";
      placeholderEl.classList.remove("hidden");

      // Show only the task name field; pre-fill the others from PAR data
      manualFields.classList.remove("hidden");
      if (document.getElementById("rtc-proj-name"))
        document.getElementById("rtc-proj-name").value = d.project_name || "";
      if (document.getElementById("rtc-task-name"))
        document.getElementById("rtc-task-name").value = "";  // only unknown field
      if (document.getElementById("rtc-customer"))
        document.getElementById("rtc-customer").value  = d.project_customer || "";
      _autoFillDepartment(d.project_organisation);
      // Pre-select PD and PM in their dropdowns once dept is set
      _preselectPerson("rtc-pd", d.project_director);
      _preselectPerson("rtc-pm", d.project_manager);

    } else {
      // No match at all — full placeholder, all fields manual
      resultEl.classList.add("hidden");
      placeholderEl.innerHTML = "<strong>No Horizon record found.</strong> Please complete all the "
        + "fields below \u2014 they will be overwritten automatically once the Horizon record "
        + "becomes available and is linked to this RTC.";
      placeholderEl.classList.remove("hidden");
      manualFields.classList.remove("hidden");
    }
  } catch(e) { console.error("Project lookup failed:", e); }
}

function _autoFillDepartment(organisation) {
  if (!organisation) return;
  const sel = document.getElementById("rtc-department");
  for (const opt of sel.options) {
    if (opt.value === organisation) {
      sel.value = organisation;
      // Trigger the onchange to populate PD/PM dropdowns
      sel.dispatchEvent(new Event("change"));
      break;
    }
  }
}

function _preselectPerson(selectId, name) {
  if (!name) return;
  const sel = document.getElementById(selectId);
  // Wait a tick for populatePersonDropdown to finish
  setTimeout(() => {
    for (const opt of sel.options) {
      if (opt.value === name) { sel.value = name; break; }
    }
  }, 50);
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
  const customer  = document.getElementById("rtc-customer")?.value.trim() || "";
  const pd        = document.getElementById("rtc-pd")?.value.trim() || "";
  const pm        = document.getElementById("rtc-pm")?.value.trim() || "";
  const errorEl   = document.getElementById("rtc-form-error");
  const submitBtn = document.getElementById("rtc-modal-submit");

  const isFullMatch       = !document.getElementById("rtc-lookup-result")?.classList.contains("hidden");
  const isProjectOnly     = isFullMatch && document.getElementById("rtc-lookup-task")?.textContent.startsWith("New task");
  const isFullPlaceholder = document.getElementById("rtc-lookup-placeholder") &&
    !document.getElementById("rtc-lookup-placeholder").classList.contains("hidden") &&
    !isProjectOnly;

  const errors = [];
  if (!projNum)   errors.push("Project number is required.");
  if (!taskNum && isFullMatch)errors.push("Task order number is required.");
  if (!startDate) errors.push("Start month is required.");
  if (!dept)      errors.push("Cost centre is required.");
  if (!pd)        errors.push("Project Director is required.");
  if (!pm)        errors.push("Project Manager is required.");

  // For project_only: task name is the only unknown field
  if (isProjectOnly && !taskName) {
    errors.push("Task name is required.");
  }

  // For full placeholder: project name, task name and customer all required
  if (isFullPlaceholder) {
    if (!projName) errors.push("Project name is required.");
    if (!taskName) errors.push("Task name is required.");
    if (!customer) errors.push("Project customer is required.");
  }

  if (errors.length) {
    errorEl.textContent = errors.join(" ");
    errorEl.classList.remove("hidden");
    return;
  }

  errorEl.classList.add("hidden");
  submitBtn.disabled = true;
  submitBtn.textContent = "Saving\u2026";

  const body = {
    project_number:      projNum,
    task_order_number:   taskNum || "",
    start_date:          startDate,
    department:          dept,
    project_name:        projName || undefined,
    task_name:           taskName || undefined,
    project_customer:    customer || undefined,
    project_director:    pd       || undefined,
    project_manager:     pm       || undefined,
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

function updateSortIndicators(view, cols) {
  const { col, dir } = state.sort[view];
  cols.forEach(c => {
    const el = document.getElementById(`sort-${view}-${c}`);
    if (!el) return;
    if (c === col) {
      el.textContent = dir === "asc" ? " ▲" : " ▼";
      el.style.color = "var(--wsp-red)";
    } else {
      el.textContent = "";
      el.style.color = "";
    }
  });
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
  ["filter-department", "filter-job-title", "filter-job-function", "filter-line-manager"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => {
      const key = id.replace("filter-", "").replace(/-/g, "_");
      state.filters[key] = el.value;
      if (id === "filter-department" && state.activeView === "projects") {
        loadRtcs();
      } else {
        renderView();
      }
    });
  });

  const horizonSel = document.getElementById("filter-horizon");
  if (horizonSel) {
    horizonSel.addEventListener("change", () => {
      state.filters.horizon = horizonSel.value;
      renderProjectTable();
    });
  }

document.getElementById("filter-project-pm")?.addEventListener("change", e => {
    state.filters.project_pm = e.target.value;
    renderView();
  });

  document.getElementById("filter-project-pd")?.addEventListener("change", e => {
    state.filters.project_pd = e.target.value;
    renderView();
  });

  const searchEl = document.getElementById("filter-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      state.filters.search = searchEl.value.trim();
      if (["rtcs","projects"].includes(state.activeView)) loadRtcs();
      else renderView();
    });
  }

  // Reset filters
  document.getElementById("filter-reset")?.addEventListener("click", resetFilters);

  // RTC-specific filters
  document.getElementById("filter-rtc-pm")?.addEventListener("change", e => {
    state.rtcFilters.pm = e.target.value === "all" ? "" : e.target.value;
    if (["rtcs","projects"].includes(state.activeView)) loadRtcs();
  });
  document.getElementById("filter-rtc-pd")?.addEventListener("change", e => {
    state.rtcFilters.pd = e.target.value === "all" ? "" : e.target.value;
    if (["rtcs","projects"].includes(state.activeView)) loadRtcs();
  });
  document.getElementById("filter-rtc-status")?.addEventListener("change", e => {
    state.rtcFilters.status = e.target.value;
    loadRtcs();
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

  // Project lookup — only fires when BOTH fields are non-empty
  // and the user moves focus away from the task number field or
  // clicks elsewhere below. This prevents the "no record" warning
  // from appearing before the user has had a chance to enter both.
  const lookupTrigger = () => {
    const projNum = document.getElementById("rtc-proj-number")?.value.trim();
    const taskNum = document.getElementById("rtc-task-number")?.value.trim();
    if (projNum && taskNum) {
      triggerProjectLookup(projNum, taskNum);
    } else {
      clearProjectLookup();
    }
  };
  // Only trigger on blur of task number (the second field), or on
  // blur of project number IF task number already has a value
  document.getElementById("rtc-task-number")?.addEventListener("blur", lookupTrigger);
  document.getElementById("rtc-proj-number")?.addEventListener("blur", () => {
    const taskNum = document.getElementById("rtc-task-number")?.value.trim();
    if (taskNum) lookupTrigger();
  });

  // Month picker year arrows
  document.getElementById("rtc-year-prev")?.addEventListener("click", () => {
    if (_rtcPickerYear > new Date().getFullYear()) {
      _rtcPickerYear--; renderMonthPicker();
    }
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
/**
 * rtc.js
 * RTC Editing Screen
 */

// ── State ────────────────────────────────────────────────────────────────────

const state = {
  rtc:          null,   // RTC metadata + project details
  periods:      [],     // Array of {period_start, label, working_days}
  staff:        [],     // Array of person objects with allocations
  allStaff:     [],     // Full staff list from /api/staff for search
  saving:       false,
  saveTimer:    null,
  dirtyCells:   {},    // key: "pid|period" -> {pid, period, days}
};

// Today's first-of-month — used to determine past/current/future months
const TODAY_MONTH = (() => {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  return `${y}-${m}-01`;
})();

// ── Initialise ───────────────────────────────────────────────────────────────

// Flush dirty cells before page unload
window.addEventListener('beforeunload', () => {
  const cells = Object.values(state.dirtyCells);
  if (!cells.length) return;
  navigator.sendBeacon(`/api/rtcs/${RTC_ID}`, JSON.stringify({
    allocations: cells.map(c => ({
      horizon_person_number: c.pid,
      period_start: c.period,
      days: c.days
    }))
  }));
});

document.addEventListener('DOMContentLoaded', async () => {
  await loadRtc();
  await loadAllStaff();
  fetch(`/api/rtcs/${RTC_ID}/opened`, { method: 'POST' }); // fire and forget
  renderHeader();
  renderGrid();
  setTimeout(scrollToCurrentMonth, 100);
  checkHorizon();
  wireEvents();
});

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadRtc() {
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    state.rtc     = d.rtc;
    state.periods = d.periods;
    state.staff   = d.staff.sort((a, b) => {
      const aGeneric = a.horizon_person_number.startsWith('GENERIC-');
      const bGeneric = b.horizon_person_number.startsWith('GENERIC-');
      if (aGeneric && !bGeneric) return 1;
      if (!aGeneric && bGeneric) return -1;
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
  } catch(e) {
    document.getElementById('header-title').textContent = 'Failed to load RTC';
    console.error(e);
  }
}

async function loadAllStaff() {
  try {
    const r = await fetch('/api/staff');
    if (!r.ok) return;
    state.allStaff = await r.json();
  } catch(e) {
    console.error('Failed to load staff list', e);
  }
}

// ── Header rendering ──────────────────────────────────────────────────────────

function renderHeader() {
  const rtc = state.rtc;
  if (!rtc) return;

  const isLinked = rtc.project_status === 'Active';
  const isPlaceholder = !isLinked;

  // Title — project name
  document.getElementById('header-title').textContent =
    rtc.project_name || 'No project name';

  // Badge
  const badge = document.getElementById('horizon-badge');
  badge.classList.remove('hidden');
  if (isLinked) {
    badge.className = 'rtc-badge rtc-badge--linked';
    badge.textContent = 'Linked to Horizon';
  } else {
    badge.className = 'rtc-badge rtc-badge--placeholder';
    badge.textContent = 'Not linked to Horizon';
  }

  // Fields
  const container = document.getElementById('header-fields');

  if (isLinked) {
    // Read-only display
    container.innerHTML = `
      ${field('Project number', rtc.project_number || '—')}
      ${field('Task order', rtc.task_order_number || '—')}
      ${field('Project name', rtc.project_name || '—')}
      ${field('Task name', rtc.task_name || '—')}
      ${field('Customer', rtc.project_customer || '—')}
      ${field('Project Director', rtc.project_director || '—')}
      ${field('Project Manager', rtc.project_manager || '—')}
      ${field('Department', rtc.department || '—')}
    `;
  } else {
    // Editable inputs for placeholder fields
    container.innerHTML = `
      <div class="rtc-field">
        <span class="rtc-field__label">Project number</span>
        <input class="rtc-field__input" id="field-proj-number"
               value="${esc((rtc.project_number || '').replace(/_\d{8}T\d+$/, ''))}"
               placeholder="e.g. UK0041867">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Task order</span>
        <input class="rtc-field__input" id="field-task-order"
                value="${esc((rtc.task_order_number || '').replace(/_\d{8}T\d+$/, ''))}"
               placeholder="e.g. 9081">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Project name</span>
        <input class="rtc-field__input" id="field-proj-name"
               value="${esc(rtc.project_name || '')}"
               placeholder="Enter project name">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Task name</span>
        <input class="rtc-field__input" id="field-task-name"
               value="${esc(rtc.task_name || '')}"
               placeholder="Enter task name">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Customer</span>
        <input class="rtc-field__input" id="field-customer"
               value="${esc(rtc.project_customer || '')}"
               placeholder="Enter customer">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Project Director</span>
        <input class="rtc-field__input" id="field-pd"
               value="${esc(rtc.project_director || '')}"
               placeholder="Enter PD name">
      </div>
      <div class="rtc-field">
        <span class="rtc-field__label">Project Manager</span>
        <input class="rtc-field__input" id="field-pm"
               value="${esc(rtc.project_manager || '')}"
               placeholder="Enter PM name">
      </div>
      ${field('Department', rtc.department || '—')}
    `;

    // Wire up lookup trigger on blur of project number + task order
    const triggerLookup = () => {
      const pn = document.getElementById('field-proj-number')?.value.trim();
      const to = document.getElementById('field-task-order')?.value.trim();
      if (pn && to) horizonLookup(pn, to);
    };
    document.getElementById('field-proj-number')?.addEventListener('blur', () => {
      const to = document.getElementById('field-task-order')?.value.trim();
      if (to) triggerLookup();
    });
    document.getElementById('field-task-order')?.addEventListener('blur', triggerLookup);

    // Wire up save on blur of manual fields
    ['field-proj-name','field-task-name','field-customer','field-pd','field-pm']
      .forEach(id => {
        document.getElementById(id)?.addEventListener('blur', saveHeaderFields);
      });
  }
}

function field(label, value) {
  return `<div class="rtc-field">
    <span class="rtc-field__label">${esc(label)}</span>
    <span class="rtc-field__value">${esc(value)}</span>
  </div>`;
}

async function saveHeaderFields() {
  const body = {
    project_name:     document.getElementById('field-proj-name')?.value.trim() || '',
    task_name:        document.getElementById('field-task-name')?.value.trim() || '',
    project_customer: document.getElementById('field-customer')?.value.trim() || '',
    project_director: document.getElementById('field-pd')?.value.trim() || '',
    project_manager:  document.getElementById('field-pm')?.value.trim() || '',
  };
  setSaveStatus('saving');
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) setSaveStatus('saved');
    else setSaveStatus('error');
  } catch(e) { setSaveStatus('error'); }
}

// ── Horizon lookup (placeholder header) ──────────────────────────────────────

async function horizonLookup(projNum, taskOrder) {
  try {
    const r = await fetch(
      `/api/project?project_number=${encodeURIComponent(projNum)}&task_order_number=${encodeURIComponent(taskOrder)}`
    );
    const d = await r.json();

    const msgEl = document.getElementById('horizon-msg');

    if (d.match_type === 'full' || d.match_type === 'project_only') {
      msgEl.className = 'rtc-horizon-msg rtc-horizon-msg--found';
      if (d.match_type === 'project_only') {
        msgEl.innerHTML = `<strong>Project found in Horizon</strong> — this task order is not yet in PAR. Project-level details have been auto-filled and will update automatically when the task order appears in the next PAR import.`;
      } else {
        window._horizonMatch = d;
        msgEl.innerHTML = `<strong>Horizon record found:</strong> ${esc(d.project_name || '')}
          <button class="btn btn--sm" style="margin-left:12px" onclick="confirmHorizonLink(window._horizonMatch)">
            Confirm link
          </button>`;
      }
      msgEl.classList.remove('hidden');
    } else {
      msgEl.className = 'rtc-horizon-msg rtc-horizon-msg--notfound';
      msgEl.textContent = 'No Horizon record found for this project number and task order.';
      msgEl.classList.remove('hidden');
    }
  } catch(e) { console.error('Horizon lookup failed', e); }
}

async function confirmHorizonLink(matchData) {
  setSaveStatus('saving');
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}/link-horizon`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_number:    matchData.project_number,
        task_order_number: matchData.task_order_number,
      }),
    });
    if (r.ok) {
      setSaveStatus('saved');
      // Reload and re-render header in linked mode
      await loadRtc();
      renderHeader();
      document.getElementById('horizon-msg').classList.add('hidden');
    } else {
      setSaveStatus('error');
    }
  } catch(e) { setSaveStatus('error'); }
}

// ── Horizon check on load ─────────────────────────────────────────────────────

async function checkHorizon() {
  const rtc = state.rtc;
  if (!rtc) return;

  const isLinked = rtc.project_status === 'Active';

  if (isLinked) {
    // Check if this was auto-linked and needs user confirmation
    try {
      const r = await fetch(`/api/rtcs/${RTC_ID}/check-horizon`);
      const d = await r.json();
      if (d.auto_linked) {
        showPopup(
          'Automatically linked to Horizon',
          `This RTC was automatically linked to a Horizon record: <strong>${esc(state.rtc.project_name || '')}</strong>.<br><br>` +
          'Please confirm this is correct. If it is not, contact your system administrator.',
          [{ label: 'Confirmed — this is correct', action: async () => {
            closePopup();
            await fetch(`/api/rtcs/${RTC_ID}/clear-auto-link`, { method: 'POST' });
          }}]
        );
      }
    } catch(e) { console.error('Auto-link check failed', e); }
    return;
  }

  // Check if a match is now available
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}/check-horizon`);
    const d = await r.json();

    if (!d.is_placeholder) return;

    if (d.match) {
      // Match found — offer to link
      showPopup(
        'Horizon record found',
        `A Horizon record matching this RTC has been found: <strong>${esc(d.match.project_name)}</strong>.<br>
         Click Confirm to link this RTC and update the project details automatically.`,
        [
          { label: 'Cancel', secondary: true, action: closePopup },
          { label: 'Confirm', action: async () => {
            closePopup();
            await confirmHorizonLink(d.match);
          }},
        ]
      );
    } else {
      // No match — remind user to update placeholder
      showPopup(
        '⚠ Action required: Link to Horizon',
        'This RTC is not linked to a Horizon record. Time allocated to it will not generate revenue.<br><br>Once the project number and task order are available, enter them in the fields above and the system will link this RTC automatically.',
        [{ label: 'I understand — I will update this when the details are available', action: closePopup }]
      );
    }
  } catch(e) { console.error('Horizon check failed', e); }
}

// ── Popup helpers ─────────────────────────────────────────────────────────────

function showPopup(title, body, buttons) {
  document.getElementById('horizon-popup-title').textContent = title;
  document.getElementById('horizon-popup-body').innerHTML = body;

  const footer = document.querySelector('#horizon-popup .rtc-popup__footer');
  footer.innerHTML = '';
  buttons.forEach(btn => {
    const el = document.createElement('button');
    el.className = btn.secondary ? 'btn btn--secondary' : 'btn';
    el.textContent = btn.label;
    el.onclick = btn.action;
    footer.appendChild(el);
  });

  document.getElementById('horizon-popup').classList.remove('hidden');
}

function closePopup() {
  document.getElementById('horizon-popup').classList.add('hidden');
}

// ── Grid rendering ────────────────────────────────────────────────────────────

function renderGrid() {
  renderGridHead();
  renderGridBody();
  sizeGridWrap();
  syncFrozenOffsets();
}

function syncFrozenOffsets() {
  const table = document.getElementById('rtc-grid');
  if (!table) return;
  const th1 = table.querySelector('th.frozen-1');
  const th2 = table.querySelector('th.frozen-2');
  const th3 = table.querySelector('th.frozen-3');
  if (!th1 || !th2 || !th3) return;
  const c1 = th1.getBoundingClientRect().width;
  const c2 = th2.getBoundingClientRect().width;
  const c3 = th3.getBoundingClientRect().width;
  table.style.setProperty('--off-2', c1 + 'px');
  table.style.setProperty('--off-3', (c1 + c2) + 'px');
  table.style.setProperty('--off-4', (c1 + c2 + c3) + 'px');
}

function sizeGridWrap() {
  const wrap = document.querySelector('.rtc-grid-wrap');
  if (!wrap) return;
  wrap.style.maxHeight =
    (window.innerHeight - wrap.getBoundingClientRect().top - 24) + 'px';
}
window.addEventListener('resize', sizeGridWrap);

function renderGridHead() {
  const table = document.getElementById('rtc-grid');
  let cg = table.querySelector('colgroup');
  if (cg) cg.remove();
  cg = document.createElement('colgroup');
  cg.innerHTML =
    '<col class="c-1"><col class="c-2"><col class="c-3"><col class="c-4">' +
    state.periods.map(() => '<col class="c-month">').join('');
  table.prepend(cg);

  const head = document.getElementById('grid-head');
  const cells = [
    '<th class="frozen frozen-1" style="text-align:center">Action</th>',
    '<th class="frozen frozen-2">Name</th>',
    '<th class="frozen frozen-3">Job Title</th>',
    '<th class="frozen frozen-4">Job Function</th>',
    ...state.periods.map(p => {
      const isCurrent = p.period_start === TODAY_MONTH;
      const shortLabel = p.label.replace(/(\w{3})-(\d{4})/, (_, m, y) => `${m}-${y.slice(2)}`);
      return `<th class="month-col${isCurrent ? ' month-current' : ''}" data-period="${p.period_start}">
        ${esc(shortLabel)}
      </th>`;
    }),
  ];
  head.innerHTML = `<tr>${cells.join('')}</tr>`;
}

function renderGridBody() {
  const tbody = document.getElementById('grid-body');

  if (!state.staff.length) {
    tbody.innerHTML = `<tr><td colspan="${3 + state.periods.length + 1}" class="rtc-empty">
      No staff added yet. Use the search box above to add people.
    </td></tr>`;
    return;
  }

  tbody.innerHTML = state.staff.map((person, idx) => {
    const isGeneric = person.horizon_person_number.startsWith('GENERIC-');
    const nameCls = isGeneric ? 'rtc-staff-name rtc-staff-name--generic' : 'rtc-staff-name';

    const actionsBtns = isGeneric
      ? `<button class="rtc-replace-btn" onclick="openReplacePopup('${esc(person.horizon_person_number)}')">Replace</button>
         <button class="rtc-remove-btn" onclick="removeStaff('${esc(person.horizon_person_number)}')">✕</button>`
      : `<button class="rtc-remove-btn" onclick="removeStaff('${esc(person.horizon_person_number)}')">✕</button>`;

    const cells = [
      `<td class="frozen frozen-1"><div class="rtc-row-actions">${actionsBtns}</div></td>`,
      `<td class="frozen frozen-2"><span class="${nameCls}">${esc(person.name)}</span></td>`,
      `<td class="frozen frozen-3"><span class="rtc-staff-job">${esc(person.job_title || '—')}</span></td>`,
      `<td class="frozen frozen-4"><span class="rtc-staff-job">${esc(person.job_function || '—')}</span></td>`,
      ...state.periods.map(p => {
        const isPast = p.period_start < TODAY_MONTH;
        const days   = person.allocations[p.period_start] ?? 0;
        const cls    = isPast ? 'rtc-cell rtc-cell--past' : (days === 0 ? 'rtc-cell rtc-cell--zero' : 'rtc-cell');
        if (isPast) {
          return `<td class="month-col"><div class="${cls}">${days > 0 ? fmt(days) : ''}</div></td>`;
        }
        return `<td class="month-col">
          <div class="${cls}"
               data-pid="${esc(person.horizon_person_number)}"
               data-period="${p.period_start}"
               onclick="startEdit(this)">
            ${days > 0 ? fmt(days) : ''}
          </div>
        </td>`;
      }),
    ];

    return `<tr data-pid="${esc(person.horizon_person_number)}">${cells.join('')}</tr>`;
  }).join('');
}

function fmt(days) {
  return Number.isInteger(days) ? String(days) : parseFloat(days.toFixed(2));
}

// ── Scroll to current month ───────────────────────────────────────────────────

function scrollToCurrentMonth() {
  const th = document.querySelector('.rtc-grid th.month-current');
  if (th) {
    const wrap = document.querySelector('.rtc-grid-wrap');
    wrap.scrollLeft = Math.max(0, th.offsetLeft - 380 - 72);
  }
}

// ── Cell editing ──────────────────────────────────────────────────────────────

let _editingCell = null;
let _navigating  = false;

function findAdjacentCell(div, rowDelta, colDelta) {
  const currentRow = div.closest('tr');
  const currentTd  = div.closest('td');
  if (!currentRow || !currentTd) return null;

  const allRows = [...document.querySelectorAll('#grid-body tr')];
  const allTds  = [...currentRow.querySelectorAll('td.month-col')];
  const colIdx  = allTds.indexOf(currentTd);
  const rowIdx  = allRows.indexOf(currentRow);

  if (colDelta !== 0) {
    const nextTd = allTds[colIdx + colDelta];
    return nextTd?.querySelector('.rtc-cell:not(.rtc-cell--past)') || null;
  }
  if (rowDelta !== 0) {
    const nextRow = allRows[rowIdx + rowDelta];
    if (!nextRow) return null;
    const nextTds = [...nextRow.querySelectorAll('td.month-col')];
    const nextTd  = nextTds[colIdx];
    return nextTd?.querySelector('.rtc-cell:not(.rtc-cell--past)') || null;
  }
  return null;
}

function startEdit(div) {
  if (div.classList.contains('rtc-cell--past')) return;
  if (_editingCell) commitEdit(_editingCell);

  _editingCell = div;
  const pid    = div.dataset.pid;
  const period = div.dataset.period;
  const person = state.staff.find(s => s.horizon_person_number === pid);
  const current = person?.allocations[period] ?? 0;

  div.classList.add('rtc-cell--editing');
  div.innerHTML = `<input class="rtc-cell-input" type="number" min="0" step="0.5"
    value="${current === 0 ? '' : current}"
    id="cell-input">`;

  const input = document.getElementById('cell-input');
  input.focus();
  input.select();

  input.addEventListener('blur', () => { if (!_navigating) commitEdit(div); });

  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { cancelEdit(div, current); return; }

    let nextDiv = null;

    if (e.key === 'Tab') {
      e.preventDefault();
      nextDiv = findAdjacentCell(div, 0, e.shiftKey ? -1 : 1);

    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      nextDiv = findAdjacentCell(div, 0, 1);

    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      nextDiv = findAdjacentCell(div, 0, -1);

    } else if (e.key === 'ArrowDown' || e.key === 'Enter') {
      e.preventDefault();
      nextDiv = findAdjacentCell(div, 1, 0);

    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      nextDiv = findAdjacentCell(div, -1, 0);
    }

    if (nextDiv) {
      _navigating = true;
      const ok = commitEdit(div);
      _navigating = false;
      if (ok !== false) startEdit(nextDiv);
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      // No cell to move to — commit and exit edit mode
      input.blur();
    }
  });
}

function cancelEdit(div, original) {
  _editingCell = null;
  div.classList.remove('rtc-cell--editing');
  const days = original ?? 0;
  div.innerHTML = days > 0 ? fmt(days) : '';
  if (days === 0) div.classList.add('rtc-cell--zero');
  else div.classList.remove('rtc-cell--zero');
}

function commitEdit(div) {
  if (!div) return;
  _editingCell = null;

  const input = div.querySelector('.rtc-cell-input');
  if (!input) return;

  const pid    = div.dataset.pid;
  const period = div.dataset.period;
  let   days   = parseFloat(input.value);
  if (isNaN(days) || days < 0) days = 0;
  days = Math.round(days * 100) / 100;

  // Cap at working days for the period
  const periodObj = state.periods.find(p => p.period_start === period);
  const maxDays = periodObj ? periodObj.working_days : 25;
  if (days > maxDays) {
    const person = state.staff.find(s => s.horizon_person_number === pid);
    const prevDays = person?.allocations[period] ?? 0;
    div.classList.remove('rtc-cell--editing');
    div.innerHTML = prevDays > 0 ? fmt(prevDays) : '';
    div.classList.toggle('rtc-cell--zero', prevDays === 0);
    showPopup(
      'Too many days',
      `You cannot allocate more than ${maxDays} days in this period. The entry has been reverted.`,
      [{ label: 'OK', action: closePopup }]
    );
    return false;
  }

  // Update local state
  const person = state.staff.find(s => s.horizon_person_number === pid);
  if (person) person.allocations[period] = days;

  // Update cell display
  div.classList.remove('rtc-cell--editing');
  div.innerHTML = days > 0 ? fmt(days) : '';
  div.classList.toggle('rtc-cell--zero', days === 0);

  // Add to dirty cells buffer and debounce a flush
  state.dirtyCells[`${pid}|${period}`] = { pid, period, days };
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushDirtyCells, 500);
  return true;
}

async function flushDirtyCells() {
  const cells = Object.values(state.dirtyCells);
  if (!cells.length) return;
  state.dirtyCells = {};
  setSaveStatus('saving');
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        allocations: cells.map(c => ({
          horizon_person_number: c.pid,
          period_start: c.period,
          days: c.days
        }))
      }),
    });
    setSaveStatus(r.ok ? 'saved' : 'error');
    if (!r.ok) {
      // Re-queue failed cells
      cells.forEach(c => state.dirtyCells[`${c.pid}|${c.period}`] = c);
    }
  } catch(e) {
    setSaveStatus('error');
    cells.forEach(c => state.dirtyCells[`${c.pid}|${c.period}`] = c);
  }
}

async function saveAllocation(pid, period, days) {
  state.dirtyCells[`${pid}|${period}`] = { pid, period, days };
  await flushDirtyCells();
}

// ── Save status ───────────────────────────────────────────────────────────────

let _saveStatusTimer = null;
function setSaveStatus(status) {
  const el = document.getElementById('save-status');
  el.className = `save-status ${status}`;
  el.textContent = { saving: 'Saving…', saved: 'Saved', error: 'Error saving' }[status] || '';
  clearTimeout(_saveStatusTimer);
  if (status === 'saved') {
    _saveStatusTimer = setTimeout(() => { el.textContent = ''; el.className = 'save-status'; }, 2000);
  }
}

// ── Staff management ──────────────────────────────────────────────────────────

async function removeStaff(pid) {
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}/staff/${encodeURIComponent(pid)}`, {
      method: 'DELETE'
    });
    if (r.ok) {
      state.staff = state.staff.filter(s => s.horizon_person_number !== pid);
      renderGridBody();
    }
  } catch(e) { console.error('Remove staff failed', e); }
}

async function addStaff(pid) {
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}/staff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ horizon_person_number: pid }),
    });
    if (r.ok) {
      // Reload full RTC to get the new staff row with correct periods
      await loadRtc();
      renderGridBody();
    }
  } catch(e) { console.error('Add staff failed', e); }
}

// ── Replace generic ───────────────────────────────────────────────────────────

let _replacingPid = null;

function openReplacePopup(pid) {
  _replacingPid = pid;
  document.getElementById('replace-search').value = '';
  document.getElementById('replace-dropdown').innerHTML = '';
  document.getElementById('replace-dropdown').classList.remove('open');
  document.getElementById('replace-popup').classList.remove('hidden');
  document.getElementById('replace-search').focus();
}

function closeReplacePopup() {
  _replacingPid = null;
  document.getElementById('replace-popup').classList.add('hidden');
}

async function replaceStaff(newPid) {
  closeReplacePopup();
  try {
    const r = await fetch(
      `/api/rtcs/${RTC_ID}/staff/${encodeURIComponent(_replacingPid || '')}/replace`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_horizon_person_number: newPid }),
      }
    );
    if (r.ok) {
      await loadRtc();
      renderGridBody();
    }
  } catch(e) { console.error('Replace staff failed', e); }
}

// ── Staff search (type-ahead) ─────────────────────────────────────────────────

function filterStaff(query, excludePids = []) {
  const q = query.toLowerCase().trim();
  const all = state.allStaff;
  const onRtc = new Set(excludePids);

  const real    = all.filter(s => !s.horizon_person_number.startsWith('GENERIC-') && !onRtc.has(s.horizon_person_number));
  const generics = all.filter(s => s.horizon_person_number.startsWith('GENERIC-'));

  const matchReal = q
    ? real.filter(s => s.name.toLowerCase().includes(q))
    : real;
  matchReal.sort((a,b) => a.name.localeCompare(b.name));

  const matchGenerics = q
    ? generics.filter(s => s.name.toLowerCase().includes(q))
    : generics;
  matchGenerics.sort((a,b) => {
    if (a.horizon_person_number === 'GENERIC-UK-DOCUMENT-CONTROL') return 1;
    if (b.horizon_person_number === 'GENERIC-UK-DOCUMENT-CONTROL') return -1;
    const gradeSort = t => {
      const m = t.match(/^([PT])(\d+)/);
      if (!m) return 999;
      return (m[1] === 'P' ? 0 : 1) * 100 + (99 - parseInt(m[2]));
    };
    return gradeSort(a.job_title||'') - gradeSort(b.job_title||'');
  });

  return { real: matchReal, generics: matchGenerics };
}

function renderDropdown(dropdownEl, query, excludePids, onSelect) {
  const { real, generics } = filterStaff(query, excludePids);
  const total = real.length + generics.length;

  if (!query && total === 0) {
    dropdownEl.classList.remove('open');
    return;
  }

  const items = [];
  real.slice(0, 20).forEach(s => {
    items.push(`<div class="rtc-staff-option" data-pid="${esc(s.horizon_person_number)}">
      <span class="rtc-staff-option__name">${esc(s.name)}</span>
      <span class="rtc-staff-option__meta">${esc(s.job_title||'')} · ${esc(s.department||'')}</span>
    </div>`);
  });

  if (generics.length) {
    items.push(`<div class="rtc-staff-option--divider">Generic roles</div>`);
    generics.forEach(s => {
      items.push(`<div class="rtc-staff-option rtc-staff-option--generic" data-pid="${esc(s.horizon_person_number)}">
        <span class="rtc-staff-option__name">${esc(s.name)}</span>
        <span class="rtc-staff-option__meta">${esc(s.job_title||'')}</span>
      </div>`);
    });
  }

  if (!items.length) {
    dropdownEl.innerHTML = `<div class="rtc-staff-option" style="color:var(--text-tertiary)">No results</div>`;
  } else {
    dropdownEl.innerHTML = items.join('');
    dropdownEl.querySelectorAll('.rtc-staff-option[data-pid]').forEach(el => {
      el.addEventListener('mousedown', e => {
        e.preventDefault();
        onSelect(el.dataset.pid);
      });
    });
  }

  dropdownEl.classList.add('open');
}

// ── Extend RTC ──────────────────────────────────────────────────────────────

async function extendRtc() {
  const btn = document.getElementById("btn-extend");
  btn.disabled = true;
  btn.textContent = "Adding\u2026";
  try {
    const r = await fetch(`/api/rtcs/${RTC_ID}/extend`, { method: "POST" });
    const d = await r.json();
    if (r.ok) {
      await loadRtc();
      renderGrid();
      setSaveStatus("saved");
    } else {
      alert(d.error || "Could not extend RTC");
    }
  } catch(e) {
    alert("Could not reach the server");
  }
  btn.disabled = false;
  btn.textContent = "+ 12 months";
}

// ── Wire events ───────────────────────────────────────────────────────────────

function wireEvents() {
  // Add staff search
  const search   = document.getElementById('staff-search');
  const dropdown = document.getElementById('staff-dropdown');

  search.addEventListener('input', () => {
    const excludePids = state.staff.map(s => s.horizon_person_number);
    renderDropdown(dropdown, search.value, excludePids, async (pid) => {
      search.value = '';
      dropdown.classList.remove('open');
      await addStaff(pid);
    });
  });
  search.addEventListener('blur', () => {
    setTimeout(() => dropdown.classList.remove('open'), 150);
  });
  search.addEventListener('focus', () => {
    if (search.value) {
      const excludePids = state.staff.map(s => s.horizon_person_number);
      renderDropdown(dropdown, search.value, excludePids, async (pid) => {
        search.value = '';
        dropdown.classList.remove('open');
        await addStaff(pid);
      });
    }
  });

  // Replace search
  const replaceSearch   = document.getElementById('replace-search');
  const replaceDropdown = document.getElementById('replace-dropdown');

  replaceSearch.addEventListener('input', () => {
    const excludePids = state.staff
      .filter(s => !s.horizon_person_number.startsWith('GENERIC-'))
      .map(s => s.horizon_person_number);
    renderDropdown(replaceDropdown, replaceSearch.value, excludePids, async (pid) => {
      replaceSearch.value = '';
      replaceDropdown.classList.remove('open');
      _replacingPid && await replaceStaff(pid);
    });
  });
  replaceSearch.addEventListener('blur', () => {
    setTimeout(() => replaceDropdown.classList.remove('open'), 150);
  });

  // Close replace popup
  document.getElementById('replace-cancel').addEventListener('click', closeReplacePopup);
  document.getElementById('replace-popup').addEventListener('click', e => {
    if (e.target.id === 'replace-popup') closeReplacePopup();
  });

  // Horizon popup close on overlay click
  document.getElementById('horizon-popup').addEventListener('click', e => {
    if (e.target.id === 'horizon-popup') closePopup();
  });

  document.getElementById('btn-back')?.addEventListener('click', async e => {
    e.preventDefault();
    clearTimeout(state.saveTimer);
    if (Object.keys(state.dirtyCells).length > 0) {
      await flushDirtyCells();
    }
    window.location.href = '/';
  });
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

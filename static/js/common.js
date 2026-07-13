/**
 * common.js
 * Shared utilities for dashboard.js and rtc.js.
 *
 * Load this BEFORE dashboard.js / rtc.js:
 *   <script src="/static/js/common.js"></script>
 *   <script src="/static/js/dashboard.js"></script>
 *
 * Everything lives under a single RF namespace to avoid collisions with
 * page-level globals. Convenience aliases (esc, escHtml) are exported for
 * drop-in compatibility with the existing call sites.
 */

const RF = (() => {

  // ── Placeholder / suffix detection ─────────────────────────────────────
  // Matches the timestamp suffix the server appends to unrecognised or
  // placeholder project/task numbers, e.g. "9081_20260706T09054112".
  // NOTE: the server should ultimately own this (see review §4.4) — once
  // the API emits display_number/is_placeholder fields, delete this.
  const PLACEHOLDER_RE = /_\d{8}T\d+$/;

  /** True if the value carries the server's collision-avoidance suffix. */
  function isSuffixed(s) {
    return !!(s && PLACEHOLDER_RE.test(s));
  }

  /** User-facing form of a project/task number: suffix stripped, "" if empty. */
  function displayNumber(s) {
    return s ? s.replace(PLACEHOLDER_RE, "") : "";
  }

  // ── Grade ordering ─────────────────────────────────────────────────────
  // Job titles start with a grade code: P7..P0 (professional),
  // T4..T0 (technician), L* (leadership, filter dropdown only).
  // Ordering rule everywhere: letter groups in the given order,
  // higher numbers first within a group (P7 before P1), unknown last.

  /**
   * Numeric rank for a job title. Lower = earlier in the list.
   * letters: which grade letters exist and their order, e.g. "PT" or "LPT".
   */
  function gradeRank(jobTitle, letters = "PT") {
    const m = (jobTitle || "").match(new RegExp("^([" + letters + "])(\\d+)"));
    if (!m) return 99999;
    const letterIdx = letters.indexOf(m[1]);
    return letterIdx * 1000 + (99 - parseInt(m[2], 10));
  }

  /**
   * Comparator for generic placeholder staff: grade order with
   * Document Control pinned last. Works with either summary records
   * (id) or RTC records (horizon_person_number).
   */
  function compareGenerics(a, b) {
    const idOf = x => x.id || x.horizon_person_number || "";
    if (idOf(a) === "GENERIC-UK-DOCUMENT-CONTROL") return 1;
    if (idOf(b) === "GENERIC-UK-DOCUMENT-CONTROL") return -1;
    return gradeRank(a.job_title) - gradeRank(b.job_title);
  }

  /** Comparator for job-title strings in the filter dropdown (L, P, T). */
  function compareTitles(a, b) {
    return gradeRank(a, "LPT") - gradeRank(b, "LPT");
  }

  // ── HTML escaping ──────────────────────────────────────────────────────
  // Single canonical implementation. Includes single quotes — the old
  // dashboard.js escHtml() omitted them, which mattered for values
  // interpolated into single-quoted attributes.
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ── Number formatting ──────────────────────────────────────────────────

  /** Day totals for display: 1 dp when fractional, thousands separators. */
  function fmtDays(d) {
    if (d === null || d === undefined) return "\u2014";
    const n = parseFloat(d);
    if (isNaN(n)) return "\u2014";
    const str = n % 1 === 0 ? n.toString() : n.toFixed(1);
    return str.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  /** Grid-cell values: integers bare, otherwise up to 2 dp trimmed. */
  function fmtCell(days) {
    return Number.isInteger(days) ? String(days) : parseFloat(days.toFixed(2));
  }

  /** Initials for the avatar circle from "Surname, Forename" names. */
  function initials(name) {
    if (!name) return "?";
    const parts = name.split(",").map(s => s.trim());
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return name.slice(0, 2).toUpperCase();
  }

  /** Short grade badge, e.g. "P3", from a full job title. */
  function gradeShort(jobTitle) {
    if (!jobTitle) return "";
    const m = jobTitle.match(/^(P\d|L\d|T\d)/);
    return m ? m[1] : jobTitle.slice(0, 3);
  }

  const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  return {
    PLACEHOLDER_RE, isSuffixed, displayNumber,
    gradeRank, compareGenerics, compareTitles,
    esc, fmtDays, fmtCell, initials, gradeShort,
    MONTH_ABBR,
  };
})();

// Drop-in aliases so existing call sites keep working. rtc.js and
// dashboard.js should delete their local esc()/escHtml() definitions
// (a later local `function esc(){}` declaration would silently shadow
// these, so remove the locals rather than relying on load order).
window.RF      = RF;
window.esc     = RF.esc;
window.escHtml = RF.esc;

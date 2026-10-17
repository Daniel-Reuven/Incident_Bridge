import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { renderRowList } from "./row-list.js";
import { showToast, describeEvent } from "./toast.js";
import { formatWhen } from "./datetime.js";

/*
 * Dashboard: four tabs over the same data, plus a text filter.
 *
 *   Everything   both queues + every incident (open, in progress and closed)
 *   Maintenance  the maintenance FIFO queue only - it already holds every
 *                active task (in progress first, then waiting, in order), so
 *                there is no second list and no sorting; the text filter
 *                still applies. Plus the "Pressing maintenance calls" link.
 *   Faults       the fault priority queue + active faults (the list also
 *                shows claimed faults, which have left the queue)
 *   Archive      closed incidents of both kinds, most recently closed first
 *
 * The data is fetched once per refresh (both queues + the full incident
 * list, which already includes every comment) and kept in `data`; switching
 * tabs, typing in the filter or changing a selector only re-renders it.
 * Live updates call refresh(), so a closed incident moves to Archive (and a
 * reopened one back) without reloading the page.
 *
 * The open tab is kept in the URL hash (#faults) and, with the filter text,
 * in sessionStorage - so coming back from an incident page restores both.
 */

const TABS = ["all", "maintenance", "faults", "archive"];
const TAB_TYPE = { maintenance: "maintenance", faults: "fault" };   // the one incident type a tab shows
const LIST_TITLES = { all: "All incidents", faults: "Faults", archive: "Archive" };
const LIST_HINTS = {
  all: "",
  faults: "Open and in progress - closed faults are in Archive",
  archive: "Closed incidents, most recently closed first",
};

// Sorting the list (Everything and Faults tabs). "asc" is the natural
// reading order of each key; the toggle button says what it currently does.
const STATUS_RANK = { open: 0, in_progress: 1, closed: 2 };
const ORDER_LABELS = {
  severity: { asc: "Most severe first", desc: "Least severe first" },
  status: { asc: "Open first", desc: "Closed first" },
  created_at: { asc: "Newest first", desc: "Oldest first" },
};
let sortOrder = recall("sortOrder") === "desc" ? "desc" : "asc";
const RESOLUTION_LABELS = { resolved: "Resolved", not_an_incident: "Not an incident", by_design: "By design" };

// Stale-work highlighting - uses the generator in backend/app/iterators.py via
// GET /incidents/work/stale. Declared first: refresh() reads these as soon as it runs.
let staleActive = false;
let staleInfo = new Map(); // incident id -> { minutes_since_update, type, ... } for incidents flagged stale

let currentTab = initialTab();
let data = { maintenance: { current: null, pending: [] }, faults: [], incidents: [] };

const user = await mountTopbar();
if (user) {
  wireTabs();
  wireFilters();
  wireNewIncidentDialog();
  wireDelegatedNavigation();
  wireStaleCheck();

  // Bulk seed-data import is an admin-only action (enforced again, for
  // real, by the server - see POST /incidents/import-jsonl - this is
  // just about not showing a button a non-admin would only get a 403
  // from). The button stays in the markup with `hidden` by default (see
  // dashboard.html) so there's nothing to flash-and-hide after load.
  if (user.role === "admin") {
    document.getElementById("import-jsonl-btn").hidden = false;
    wireImportJsonlDialog();
  }

  document.getElementById("start-next-btn").addEventListener("click", async () => {
    try {
      await api.startNextMaintenance();
      await refresh();
    } catch (err) {
      alert(err.message);
    }
  });
  document.getElementById("claim-next-btn").addEventListener("click", async () => {
    try {
      const fault = await api.claimNextFault();
      window.location.href = `/incident.html?id=${fault.id}`;
    } catch (err) {
      alert(err.message);
    }
  });

  showTab(currentTab);
  await refresh();

  // Any live-update event could change any list (a closed fault leaves the
  // fault queue and moves to Archive, etc.), so every event re-fetches
  // everything. renderRowList's per-row diffing (see row-list.js) leaves
  // unchanged rows untouched, so this stays flicker-free. The server
  // already excludes this tab's own client_id (see events.js /
  // backend/app/events.py), so the toast is always someone else's action.
  connectLiveUpdates((event) => {
    showToast(describeEvent(event));
    refresh();
  });
}

function severityLabel(value) {
  return { 1: "Critical", 2: "Major", 3: "Minor" }[value] || "";
}
function severityClass(value) {
  return { 1: "sev-critical", 2: "sev-major", 3: "sev-minor" }[value] || "";
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- per-viewer memory (sessionStorage can be unavailable: always optional) ---

function remember(key, value) {
  try { sessionStorage.setItem(`dashboard.${key}`, value); } catch { /* not kept - fine */ }
}
function recall(key) {
  try { return sessionStorage.getItem(`dashboard.${key}`); } catch { return null; }
}

// --- tabs ---

function initialTab() {
  const fromHash = window.location.hash.slice(1);
  if (TABS.includes(fromHash)) return fromHash;
  const remembered = recall("tab");
  return TABS.includes(remembered) ? remembered : "all";
}

function showTab(name) {
  currentTab = name;
  remember("tab", name);
  history.replaceState(null, "", `#${name}`);
  document.querySelectorAll('.tabs [role="tab"]').forEach((tab) =>
    tab.setAttribute("aria-selected", String(tab.id === `tab-${name}`)));
  document.querySelectorAll("[data-tabs]").forEach((section) => {
    section.hidden = !section.dataset.tabs.split(" ").includes(name);
  });

  // Controls that only make sense in some tabs.
  document.getElementById("filter-type-label").hidden = name !== "all";
  // Maintenance: the queue's FIFO order is the point, so no status filter and no sorting.
  document.getElementById("filter-status-label").hidden = name === "archive" || name === "maintenance";
  document.getElementById("sort-group").hidden = name === "archive" || name === "maintenance";
  document.getElementById("stale-threshold-label").hidden = name === "archive";
  document.getElementById("check-stale-btn").hidden = name === "archive";
  document.getElementById("pressing-link").hidden = name !== "maintenance";
  const status = document.getElementById("filter-status");
  status.querySelector('option[value="closed"]').hidden = name !== "all";   // closed = Archive in the type tabs
  if (name !== "all" && status.value === "closed") status.value = "";

  document.getElementById("list-title").textContent = LIST_TITLES[name] || "";
  document.getElementById("list-hint").textContent = LIST_HINTS[name] || "";
  render();
}

function wireTabs() {
  document.querySelectorAll('.tabs [role="tab"]').forEach((tab) =>
    tab.addEventListener("click", () => showTab(tab.id.replace("tab-", ""))));
  window.addEventListener("hashchange", () => {
    const name = window.location.hash.slice(1);
    if (TABS.includes(name) && name !== currentTab) showTab(name);
  });
}

/** Does `item` belong in `tab`'s list? (Everything: all of them; Archive: closed ones.) */
function inTab(item, tab) {
  if (tab === "all") return true;
  if (tab === "archive") return item.status === "closed";
  return item.type === TAB_TYPE[tab] && item.status !== "closed";
}

// --- text filter ---

function filterTerms() {
  return document.getElementById("filter-text").value.toLowerCase().split(/\s+/).filter(Boolean);
}

/**
 * Match `item` against the filter terms: every term must appear (case
 * insensitive) in the title, the description or a comment. Returns null
 * for no match, otherwise { inComments } - true when the terms are not all
 * in the title/description, so the row can say the match is in a comment
 * (comments are not shown on the dashboard).
 */
function matchFilter(item, terms) {
  if (terms.length === 0) return { inComments: false };
  const main = `${item.title}\n${item.description}`.toLowerCase();
  const comments = (item.comments || []).map((c) => c.text).join("\n").toLowerCase();
  if (!terms.every((t) => main.includes(t) || comments.includes(t))) return null;
  return { inComments: !terms.every((t) => main.includes(t)) };
}

function matchTag(match) {
  return match && match.inComments ? `<span class="row-match-tag">match in comments</span>` : "";
}

function emptyMessage(fallback) {
  const text = document.getElementById("filter-text").value.trim();
  return text ? `Nothing matches “${escapeHtml(text)}”.` : fallback;
}

/** renderRowList keeps an existing empty message; reset it so a changed filter text shows up. */
function renderList(container, items, keyFn, rowFn, empty) {
  if (items.length === 0) container.dataset.empty = "0";
  renderRowList(container, items, keyFn, rowFn, empty);
}

// One delegated click listener per list container, attached once, rather
// than per-row - required by renderRowList's reuse-existing-elements
// approach (see row-list.js). Each row's data-key is its incident id.
function wireDelegatedNavigation() {
  ["maintenance-queue", "fault-queue", "incident-list"].forEach((id) => {
    document.getElementById(id).addEventListener("click", (e) => {
      const row = e.target.closest("[data-key]");
      if (row) window.location.href = `/incident.html?id=${row.dataset.key}`;
    });
  });
}

// --- loading and rendering ---

async function refresh() {
  try {
    await loadStale(); // keeps the highlight correct after live updates (no-op while highlighting is off)
  } catch {
    // keep the previous highlights if the check fails - the lists themselves still refresh
  }
  const [maintenance, faults, incidents] = await Promise.all([
    api.maintenanceQueue(), api.faultQueue(), api.listIncidents(),
  ]);
  data = { maintenance, faults: faults || [], incidents };
  render();
}

function render() {
  const terms = filterTerms();
  renderCounts(terms);
  renderMaintenanceQueue(terms);
  renderFaultQueue(terms);
  renderIncidentList(terms);
}

function renderCounts(terms) {
  const matching = data.incidents.filter((item) => matchFilter(item, terms));
  for (const tab of TABS) {
    const shown = matching.filter((item) => inTab(item, tab)).length;
    const total = data.incidents.filter((item) => inTab(item, tab)).length;
    document.getElementById(`count-${tab}`).textContent = terms.length ? `${shown} / ${total}` : String(total);
  }
}

function renderMaintenanceQueue(terms) {
  const { current, pending } = data.maintenance;
  const items = [];
  // Labels use the server's queue_position - the same numbering the
  // incident page's badge shows, where an in-progress task is position 1
  // and waiting tasks follow. Positions are taken BEFORE filtering, so a
  // filtered queue still shows each task's real place in line.
  if (current) items.push({ task: current, label: `#${current.queue_position || 1} In progress` });
  pending.forEach((t, i) => items.push({ task: t, label: `#${t.queue_position || i + 1} in queue` }));
  const shown = items
    .map((item) => ({ ...item, match: matchFilter(item.task, terms) }))
    .filter((item) => item.match);

  renderList(
    document.getElementById("maintenance-queue"),
    shown,
    (item) => item.task.id,
    ({ task, label, match }) => {
      const stale = staleInfo.get(task.id);
      return {
        className: `row${stale ? " row-stale" : ""}`,
        html: `
        <span class="row-position">${label}</span>
        <span class="row-title">${escapeHtml(task.title)}</span>${staleTag(stale)}${matchTag(match)}
        <span class="row-meta">by ${escapeHtml(task.created_by)} · ${formatWhen(task.created_at)}</span>`,
        sig: JSON.stringify([label, task.title, task.updated_at, stale ? stale.minutes_since_update : null,
                             match.inComments]),
      };
    },
    emptyMessage("Nothing queued.")
  );
}

function renderFaultQueue(terms) {
  // Positions first (the real priority order), then the filter.
  const shown = data.faults
    .map((f, index) => ({ ...f, position: index + 1, match: matchFilter(f, terms) }))
    .filter((f) => f.match);

  renderList(
    document.getElementById("fault-queue"),
    shown,
    (f) => f.id,
    (f) => {
      const stale = staleInfo.get(f.id);
      return {
        className: `row ${severityClass(f.severity)}${stale ? " row-stale" : ""}`,
        html: `
        <span class="row-position">#${f.position}</span>
        <span class="row-badge">${severityLabel(f.severity)}</span>
        <span class="row-title">${escapeHtml(f.title)}</span>${staleTag(stale)}${matchTag(f.match)}
        <span class="row-meta">${formatWhen(f.created_at)}</span>`,
        sig: JSON.stringify([f.position, f.severity, f.title, f.updated_at,
                             stale ? stale.minutes_since_update : null, f.match.inComments]),
      };
    },
    emptyMessage("No faults found.")
  );
}

function renderIncidentList(terms) {
  const type = document.getElementById("filter-type").value;
  const status = document.getElementById("filter-status").value;
  const sortBy = document.getElementById("sort-by").value;
  const archive = currentTab === "archive";

  const items = data.incidents
    .filter((item) => inTab(item, currentTab))
    .filter((item) => currentTab !== "all" || !type || item.type === type)
    .filter((item) => archive || !status || item.status === status)
    .map((item) => ({ ...item, match: matchFilter(item, terms) }))
    .filter((item) => item.match);

  items.sort(archive
    ? (a, b) => new Date(b.updated_at) - new Date(a.updated_at)            // most recently closed first
    : compareBy(sortBy, sortOrder));

  renderList(
    document.getElementById("incident-list"),
    items,
    (item) => item.id,
    (item) => {
      const isFault = item.type === "fault";
      const sevClass = isFault ? severityClass(item.severity) : "";
      const badge = isFault ? severityLabel(item.severity) : "Maintenance";
      const stale = staleInfo.get(item.id);
      const state = item.status === "closed"
        ? `closed · ${RESOLUTION_LABELS[item.resolution_type] || "closed"}`
        : item.status.replace("_", " ");
      const when = archive ? `closed ${formatWhen(item.updated_at)}` : formatWhen(item.created_at);
      return {
        className: `row ${sevClass}${stale ? " row-stale" : ""}`,
        html: `
          <span class="row-badge">${badge}</span>
          <span class="row-title">${escapeHtml(item.title)}</span>${staleTag(stale)}${matchTag(item.match)}
          <span class="row-status">${state}</span>
          <span class="row-meta">${when}</span>`,
        sig: JSON.stringify([item.status, item.resolution_type, item.severity, item.title, item.updated_at,
                             stale ? stale.minutes_since_update : null, item.match.inComments, archive]),
      };
    },
    emptyMessage(archive ? "No closed incidents yet." : "No incidents match these filters.")
  );
}

// --- stale-work check ---

function formatIdle(minutes) {
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
  return `${Math.floor(minutes / 1440)}d`;
}

function staleTag(stale) {
  return stale ? `<span class="row-stale-tag">Stale · ${formatIdle(stale.minutes_since_update)} idle</span>` : "";
}

async function loadStale() {
  if (!staleActive) return null;
  const result = await api.staleIncidents(document.getElementById("stale-threshold").value);
  staleInfo = new Map(result.results.map((r) => [r.id, r]));
  return result;
}

function wireStaleCheck() {
  const button = document.getElementById("check-stale-btn");
  const threshold = document.getElementById("stale-threshold");

  button.addEventListener("click", async () => {
    if (staleActive) { // second press: switch the highlighting off again
      staleActive = false;
      staleInfo = new Map();
      button.textContent = "Check stale incidents";
      render();
      return;
    }
    showToast("Checking stale incidents…");
    staleActive = true;
    try {
      const result = await loadStale();
      button.textContent = "Hide stale highlight";
      render();
      // Count only what this tab shows (Maintenance: maintenance tasks, Faults: faults).
      const type = TAB_TYPE[currentTab];
      const count = result.results.filter((r) => !type || r.type === type).length;
      const what = type === "fault" ? "fault" : type === "maintenance" ? "maintenance task" : "incident";
      showToast(count
        ? `${count} stale ${what}${count === 1 ? "" : "s"} highlighted (no update for over ${formatIdle(result.stale_after_minutes)})`
        : `No stale ${what}s found`);
    } catch (err) {
      staleActive = false;
      staleInfo = new Map();
      alert(err.message);
    }
  });

  threshold.addEventListener("change", () => { if (staleActive) refresh(); });
}

/**
 * Comparator for the list: by `key` ("severity", "status" or "created_at"),
 * `order` "asc" (most severe / open / newest first) or "desc" (the reverse).
 * Items without a severity (maintenance tasks) always come last when
 * sorting by severity, whichever the order. Ties: newest first.
 */
function compareBy(key, order) {
  const sign = order === "desc" ? -1 : 1;
  const newestFirst = (a, b) => new Date(b.created_at) - new Date(a.created_at);
  return (a, b) => {
    let result = 0;
    if (key === "severity") {
      if (a.severity == null || b.severity == null) {
        if (a.severity != null) return -1;
        if (b.severity != null) return 1;
      } else {
        result = sign * (a.severity - b.severity);
      }
    } else if (key === "status") {
      result = sign * (STATUS_RANK[a.status] - STATUS_RANK[b.status]);
    } else {
      result = sign * newestFirst(a, b);
    }
    return result || newestFirst(a, b);
  };
}

function updateOrderButton() {
  const key = document.getElementById("sort-by").value;
  const button = document.getElementById("sort-order");
  button.textContent = `${sortOrder === "asc" ? "↓" : "↑"} ${ORDER_LABELS[key][sortOrder]}`;
  button.title = "Reverse the order";
}

function wireFilters() {
  ["filter-type", "filter-status"].forEach((id) =>
    document.getElementById(id).addEventListener("change", render)
  );
  const sortBy = document.getElementById("sort-by");
  const remembered = recall("sortBy");
  if (remembered && ORDER_LABELS[remembered]) sortBy.value = remembered;
  sortBy.addEventListener("change", () => { remember("sortBy", sortBy.value); updateOrderButton(); render(); });
  document.getElementById("sort-order").addEventListener("click", () => {
    sortOrder = sortOrder === "asc" ? "desc" : "asc";
    remember("sortOrder", sortOrder);
    updateOrderButton();
    render();
  });
  updateOrderButton();
  const text = document.getElementById("filter-text");
  text.value = recall("filter") || "";
  let timer = null;
  text.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { remember("filter", text.value); render(); }, 150);
  });
}

function wireNewIncidentDialog() {
  const dialog = document.getElementById("new-incident-dialog");
  const form = document.getElementById("new-incident-form");
  const kindSelect = document.getElementById("new-kind");
  const faultFields = document.getElementById("fault-fields");
  const errorEl = document.getElementById("new-incident-error");

  document.getElementById("new-incident-btn").addEventListener("click", () => {
    form.reset();
    faultFields.hidden = kindSelect.value !== "fault";
    errorEl.hidden = true;
    dialog.showModal();
  });
  kindSelect.addEventListener("change", () => { faultFields.hidden = kindSelect.value !== "fault"; });
  dialog.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    const title = form.title.value.trim();
    const description = form.description.value.trim();
    try {
      if (kindSelect.value === "maintenance") {
        await api.createMaintenanceTask(title, description);
      } else {
        const details = {
          system_unavailable: form.system_unavailable.checked,
          security_breach: form.security_breach.checked,
          performance_degraded: form.performance_degraded.checked,
          affected_users_percent: Number(form.affected_users_percent.value) || 0,
        };
        await api.createFault(title, description, details);
      }
      dialog.close();
      await refresh();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });
}

function wireImportJsonlDialog() {
  const dialog = document.getElementById("import-jsonl-dialog");
  const form = document.getElementById("import-jsonl-form");
  const fileInput = form.querySelector('input[name="file"]');
  const errorEl = document.getElementById("import-jsonl-error");
  const summaryEl = document.getElementById("import-jsonl-summary");
  const submitBtn = document.getElementById("import-jsonl-submit");

  document.getElementById("import-jsonl-btn").addEventListener("click", () => {
    form.reset();
    errorEl.hidden = true;
    summaryEl.hidden = true;
    dialog.showModal();
  });
  dialog.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    summaryEl.hidden = true;

    const file = fileInput.files[0];
    if (!file) return;

    submitBtn.disabled = true;
    try {
      // Read client-side and send the file's own text content, not a
      // path - the server has no way to open "a path on the admin's
      // laptop" and shouldn't need to (see backend/app/api/schemas.py's
      // ImportJsonlRequest for the same reasoning on the backend side).
      const content = await file.text();
      const result = await api.importJsonl(content);
      renderImportSummary(result);
      // The server excludes THIS tab from its own live-update broadcast
      // (same X-Client-Id mechanism every other mutating call uses - see
      // api.js/events.js) - so unlike a response to another tab's action,
      // our own queues/lists need an explicit refresh here, not just the
      // toast-triggered one connectLiveUpdates() does for everyone else.
      await refresh();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function renderImportSummary(result) {
  const summaryEl = document.getElementById("import-jsonl-summary");
  const duplicateCount = result.skipped_duplicate_ids.length;
  const invalidCount = result.skipped_invalid.length;

  const invalidListHtml = invalidCount
    ? `<ul>${result.skipped_invalid.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
    : "";

  summaryEl.innerHTML = `
    <p><strong>${result.created}</strong> incident${result.created === 1 ? "" : "s"} imported.</p>
    ${duplicateCount ? `<p>${duplicateCount} skipped (already imported).</p>` : ""}
    ${invalidCount ? `<p>${invalidCount} skipped (invalid):</p>${invalidListHtml}` : ""}
  `;
  summaryEl.hidden = false;
}
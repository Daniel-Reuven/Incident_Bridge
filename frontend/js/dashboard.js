import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { renderRowList } from "./row-list.js";
import { showToast, describeEvent } from "./toast.js";

const user = await mountTopbar();
if (user) {
  wireFilters();
  wireNewIncidentDialog();
  wireDelegatedNavigation();

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
      await loadMaintenance();
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

  await refresh();

  // Any live-update event could affect any of the three lists below (a
  // maintenance change can shift "All incidents" too, a severity change
  // reorders the fault queue, etc.) - rather than special-case which
  // event types touch which list, just refresh all three every time.
  // renderRowList's per-row diffing (see row-list.js) means an unrelated
  // list's rows are left completely untouched when nothing in them
  // actually changed, so this stays flicker-free without needing that
  // bookkeeping - the server already excludes this tab's own client_id
  // (see events.js / backend/app/events.py), so there's no need to
  // filter here too. This is also why the toast fires unconditionally
  // for every event this tab receives: those are, by construction,
  // always *someone else's* action.
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
function timeAgo(iso) {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// One delegated click listener per list container, attached once, rather
// than per-row - required by renderRowList's reuse-existing-elements
// approach (see row-list.js): a per-row listener would either need
// re-attaching after every patch (easy to forget) or would silently
// double-bind on a reused row (attaching the same handler twice), neither
// of which is right. Each row's data-key is its incident id.
function wireDelegatedNavigation() {
  ["maintenance-queue", "fault-queue", "incident-list"].forEach((id) => {
    document.getElementById(id).addEventListener("click", (e) => {
      const row = e.target.closest("[data-key]");
      if (row) window.location.href = `/incident.html?id=${row.dataset.key}`;
    });
  });
}

async function loadMaintenance() {
  const { current, pending } = await api.maintenanceQueue();
  const items = [];
  if (current) items.push({ task: current, label: "In progress" });
  pending.forEach((t, i) => items.push({ task: t, label: `#${i + 1} in queue` }));

  renderRowList(
    document.getElementById("maintenance-queue"),
    items,
    (item) => item.task.id,
    ({ task, label }) => ({
      className: "row",
      html: `
        <span class="row-position">${label}</span>
        <span class="row-title">${escapeHtml(task.title)}</span>
        <span class="row-meta">by ${escapeHtml(task.created_by)} · ${timeAgo(task.created_at)}</span>`,
      sig: JSON.stringify([label, task.title, task.updated_at]),
    }),
    "Nothing queued."
  );
}

async function loadFaultQueue() {
  const faults = await api.faultQueue();
  renderRowList(
    document.getElementById("fault-queue"),
    faults,
    (f) => f.id,
    (f, i) => ({
      className: `row ${severityClass(f.severity)}`,
      html: `
        <span class="row-position">#${i + 1}</span>
        <span class="row-badge">${severityLabel(f.severity)}</span>
        <span class="row-title">${escapeHtml(f.title)}</span>
        <span class="row-meta">${timeAgo(f.created_at)}</span>`,
      sig: JSON.stringify([i, f.severity, f.title, f.updated_at]),
    }),
    "Nothing waiting."
  );
}

async function loadIncidents() {
  const type = document.getElementById("filter-type").value;
  const status = document.getElementById("filter-status").value;
  const sortBy = document.getElementById("sort-by").value;

  const items = await api.listIncidents({ type, status });
  items.sort((a, b) => {
    if (sortBy === "severity") return (a.severity ?? 4) - (b.severity ?? 4);
    if (sortBy === "status") return a.status.localeCompare(b.status);
    return new Date(b.created_at) - new Date(a.created_at);
  });

  renderRowList(
    document.getElementById("incident-list"),
    items,
    (item) => item.id,
    (item, i) => {
      const isFault = item.type === "fault";
      const sevClass = isFault ? severityClass(item.severity) : "";
      const badge = isFault ? severityLabel(item.severity) : "Maintenance";
      return {
        className: `row ${sevClass}`,
        html: `
          <span class="row-badge">${badge}</span>
          <span class="row-title">${escapeHtml(item.title)}</span>
          <span class="row-status">${item.status.replace("_", " ")}</span>
          <span class="row-meta">${timeAgo(item.created_at)}</span>`,
        sig: JSON.stringify([i, item.status, item.severity, item.title, item.updated_at]),
      };
    },
    "No incidents match these filters."
  );
}

async function refresh() {
  await Promise.all([loadMaintenance(), loadFaultQueue(), loadIncidents()]);
}

function wireFilters() {
  ["filter-type", "filter-status", "sort-by"].forEach((id) =>
    document.getElementById(id).addEventListener("change", loadIncidents)
  );
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
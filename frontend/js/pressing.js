import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { showToast, describeEvent } from "./toast.js";

const table = document.getElementById("pressing-table");
const tbody = table.querySelector("tbody");
const emptyMsg = document.getElementById("pressing-empty");
const statsEl = document.getElementById("pressing-stats");
const limitSelect = document.getElementById("pressing-limit");

const user = await mountTopbar();
if (user) {
  limitSelect.addEventListener("change", load);
  // One delegated click listener (rows are reused, never re-created - see renderRows).
  tbody.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-key]");
    if (row) window.location.href = `/incident.html?id=${row.dataset.key}`;
  });

  await load();

  // Any live-update event could change this list (a call started, closed, created...),
  // so just reload it - renderRows only patches rows whose content changed.
  connectLiveUpdates((event) => {
    showToast(describeEvent(event));
    load();
  });
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// The backend runs a lazy three-stage pipeline (see backend/app/iterators.py)
// and stops after `limit` results; the stats line shows how many incidents it
// actually had to look at.
async function load() {
  let data;
  try {
    data = await api.pressingMaintenance(limitSelect.value);
  } catch (err) {
    statsEl.textContent = err.message;
    return;
  }
  renderRows(data.results);
  table.hidden = data.results.length === 0;
  emptyMsg.hidden = data.results.length !== 0;
  statsEl.textContent =
    `Examined ${data.examined} of ${data.total_incidents} incidents` +
    (data.stopped_early ? " — stopped early, the rest were never looked at." : ".");
}

// Same idea as row-list.js (patch rows in place by key, no full rebuild, so no
// flicker on live updates), but for <tr> elements inside a real table.
function renderRows(results) {
  const existing = new Map();
  tbody.querySelectorAll(":scope > tr[data-key]").forEach((tr) => existing.set(tr.dataset.key, tr));

  let previous = null;
  const used = new Set();
  results.forEach((call, index) => {
    used.add(call.id);
    const sig = JSON.stringify([index, call.title, call.created_by, call.days_open]);
    let tr = existing.get(call.id);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.key = call.id;
    }
    if (tr.dataset.sig !== sig) {
      tr.innerHTML = `
        <td class="num">${index + 1}</td>
        <td>${escapeHtml(call.title)}</td>
        <td>${escapeHtml(call.created_by)}</td>
        <td>${new Date(call.created_at).toLocaleDateString()}</td>
        <td>${call.days_open} days</td>`;
      tr.dataset.sig = sig;
    }
    if (previous) previous.after(tr);
    else tbody.prepend(tr);
    previous = tr;
  });
  existing.forEach((tr, key) => { if (!used.has(key)) tr.remove(); });
}

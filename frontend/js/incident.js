import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { showToast, describeEvent } from "./toast.js";

const user = await mountTopbar();
const root = document.getElementById("incident-root");
const incidentId = new URLSearchParams(window.location.search).get("id");

// Set once a live update for THIS incident has been declined (see
// handleConflict below). While true: mutating controls stay disabled and
// the page is never re-rendered, so nothing already typed (e.g. a
// half-written comment) can be silently lost - the only way back to a
// consistent view is a real page refresh, which is the point.
let locked = false;

if (user) {
  await load();
  connectLiveUpdates((event) => {
    showToast(describeEvent(event));
    if (event.id === incidentId) handleConflict();
  });
}

function severityLabel(value) { return { 1: "Critical", 2: "Major", 3: "Minor" }[value] || ""; }
function severityClass(value) { return { 1: "sev-critical", 2: "sev-major", 3: "sev-minor" }[value] || ""; }
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function formatDate(iso) { return new Date(iso).toLocaleString(); }

async function load() {
  if (!incidentId) {
    root.innerHTML = `<p class="empty">No incident selected.</p>`;
    return;
  }
  let incident;
  try {
    incident = await api.getIncident(incidentId);
  } catch (err) {
    root.innerHTML = `<p class="empty">${escapeHtml(err.message)}</p>`;
    return;
  }
  render(incident);
}

// --- conflict popup + locked banner (own DOM, outside #incident-root, so
// they're completely unaffected by render() and by lockMutatingControls()) ---

function ensureConflictDialog() {
  let dialog = document.getElementById("conflict-dialog");
  if (dialog) return dialog;

  dialog = document.createElement("dialog");
  dialog.id = "conflict-dialog";
  dialog.className = "dialog";
  dialog.innerHTML = `
    <div class="dialog-form">
      <h2>This incident changed</h2>
      <p>Another user updated this incident. Refresh to see the latest changes?</p>
      <p class="hint">If you don't refresh, you can keep viewing this page, but no actions can be
      submitted here until you do - that's to make sure nothing you're working on gets lost.</p>
      <div class="dialog-actions">
        <button type="button" class="btn btn-ghost" data-action="decline">Not now</button>
        <button type="button" class="btn btn-primary" data-action="refresh">Refresh</button>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  dialog.querySelector('[data-action="refresh"]').addEventListener("click", () => window.location.reload());
  dialog.querySelector('[data-action="decline"]').addEventListener("click", () => {
    dialog.close();
    lockMutatingControls();
    showStaleBanner();
  });
  return dialog;
}

function showStaleBanner() {
  if (document.getElementById("stale-banner")) return;
  const banner = document.createElement("div");
  banner.id = "stale-banner";
  banner.className = "stale-banner";
  banner.textContent = "Viewing outdated data — refresh the page to make changes.";
  document.body.prepend(banner);
}

function lockMutatingControls() {
  locked = true;
  root.querySelectorAll("button, select, textarea, input").forEach((el) => { el.disabled = true; });
}

function handleConflict() {
  if (locked) return; // already declined once for this incident - banner already says so
  const dialog = ensureConflictDialog();
  if (!dialog.open) dialog.showModal();
}

function render(incident) {
  const isFault = incident.type === "fault";
  const isAdmin = user.role === "admin";

  const badge = isFault
    ? `<span class="detail-badge ${severityClass(incident.severity)}">${severityLabel(incident.severity)}</span>`
    : `<span class="detail-badge">Maintenance${incident.queue_position ? ` · position ${incident.queue_position}` : ""}</span>`;

  let actionsHtml;
  if (incident.status === "closed") {
    actionsHtml = `
      <div class="resolution-panel">
        <strong>${incident.resolution_type.replace("_", " ")}</strong>
        <p>${escapeHtml(incident.resolution_message)}</p>
      </div>`;
  } else if (!isFault) {
    actionsHtml = incident.status === "in_progress"
      ? `<button class="btn btn-primary" data-action="complete">Complete</button>`
      : `<p class="hint">Waiting in the maintenance queue — tasks start strictly in order.</p>`;
  } else if (incident.status !== "in_progress") {
    actionsHtml = `<p class="hint">Waiting in the fault queue, unclaimed. Claim it from the dashboard to start work.</p>`;
  } else {
    actionsHtml = `
      <button class="btn btn-primary" data-action="resolve">Resolve</button>
      ${isAdmin ? `<button class="btn btn-ghost" data-action="not-incident">Not an incident</button>` : ""}
      ${isAdmin ? `<button class="btn btn-ghost" data-action="by-design">By design</button>` : ""}
      ${isAdmin ? `
        <label class="inline-select">Severity
          <select data-role="severity-select">
            <option value="1" ${incident.severity === 1 ? "selected" : ""}>Critical</option>
            <option value="2" ${incident.severity === 2 ? "selected" : ""}>Major</option>
            <option value="3" ${incident.severity === 3 ? "selected" : ""}>Minor</option>
          </select>
        </label>` : ""}`;
  }

  root.innerHTML = `
    <article class="incident-detail">
      <header class="incident-header">
        <div>
          <h1>${escapeHtml(incident.title)}</h1>
          <p class="incident-meta">Reported by ${escapeHtml(incident.created_by)} · ${formatDate(incident.created_at)}</p>
        </div>
        ${badge}
      </header>
      <p class="incident-description">${escapeHtml(incident.description)}</p>
      <div class="actions-row">${actionsHtml}</div>

      <section class="comments-section">
        <h2>Updates</h2>
        <div class="comments-list">
          ${incident.comments.map((c) => `
            <div class="comment">
              <span class="comment-author">${escapeHtml(c.author)}</span>
              <span class="comment-time">${formatDate(c.created_at)}</span>
              <p>${escapeHtml(c.text)}</p>
            </div>`).join("") || `<p class="empty">No updates yet.</p>`}
        </div>
        <form class="comment-form" id="comment-form">
          <textarea name="text" rows="2" placeholder="Add an update…" required></textarea>
          <button type="submit" class="btn btn-primary">Post</button>
        </form>
      </section>
    </article>

    <dialog id="close-dialog" class="dialog">
      <form id="close-form" class="dialog-form">
        <h2 id="close-dialog-title">Resolve incident</h2>
        <label>Message<textarea name="message" rows="3" required placeholder="Explain what happened…"></textarea></label>
        <p class="form-error" id="close-error" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary">Confirm</button>
        </div>
      </form>
    </dialog>
  `;

  wireActions(incident, isFault);
}

function wireActions(incident, isFault) {
  document.getElementById("comment-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (locked) return;
    const text = e.target.text.value.trim();
    if (!text) return;
    await api.addComment(incident.id, text);
    await load();
  });

  const closeDialog = document.getElementById("close-dialog");
  const closeForm = document.getElementById("close-form");
  const closeError = document.getElementById("close-error");
  let pendingResolutionType = "resolved";

  function openCloseDialog(resolutionType, title) {
    if (locked) return;
    pendingResolutionType = resolutionType;
    document.getElementById("close-dialog-title").textContent = title;
    closeError.hidden = true;
    closeForm.reset();
    closeDialog.showModal();
  }

  const bind = (selector, resolutionType, title) => {
    const btn = document.querySelector(selector);
    if (btn) btn.addEventListener("click", () => openCloseDialog(resolutionType, title));
  };
  bind('[data-action="complete"]', "resolved", "Complete task");
  bind('[data-action="resolve"]', "resolved", "Resolve fault");
  bind('[data-action="not-incident"]', "not_an_incident", "Mark as not an incident");
  bind('[data-action="by-design"]', "by_design", "Mark as by design");

  closeDialog.querySelector('[data-action="cancel"]').addEventListener("click", () => closeDialog.close());

  closeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (locked) return;
    closeError.hidden = true;
    const message = e.target.message.value.trim();
    try {
      if (isFault) {
        await api.closeFault(incident.id, pendingResolutionType, message);
      } else {
        await api.completeCurrentMaintenance(message, pendingResolutionType);
      }
      closeDialog.close();
      await load();
    } catch (err) {
      closeError.textContent = err.message;
      closeError.hidden = false;
    }
  });

  const severitySelect = document.querySelector('[data-role="severity-select"]');
  if (severitySelect) {
    severitySelect.addEventListener("change", async () => {
      if (locked) return;
      try {
        await api.changeFaultSeverity(incident.id, Number(severitySelect.value));
      } catch (err) {
        alert(err.message);
      }
      await load();
    });
  }
}

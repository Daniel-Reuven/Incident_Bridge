import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { showToast, describeEvent } from "./toast.js";
import { getUpdatesOrder, setUpdatesOrder, sortComments, onUpdatesOrderChanged } from "./updates-order.js";

const user = await mountTopbar();
const root = document.getElementById("incident-root");
const incidentId = new URLSearchParams(window.location.search).get("id");

// Set once a live update for THIS incident has been declined (see
// handleConflict below). While true: mutating controls stay disabled and
// the page is never re-rendered, so nothing already typed (e.g. a
// half-written comment) can be silently lost - the only way back to a
// consistent view is a real page refresh, which is the point.
let locked = false;

// The incident currently shown (set by load()). Used to decide whether a
// live event from ANOTHER incident could change whether this one's Claim
// button is allowed - see refreshClaimState() - and as the source for
// re-ordering the Updates list in place - see applyUpdatesOrder().
let currentIncident = null;

if (user) {
  await load();
  connectLiveUpdates((event) => {
    showToast(describeEvent(event));
    // An event concerns THIS page if it is about this incident itself, or
    // lists this incident in affected_ids (e.g. a maintenance task whose
    // queue position shifted because a task ahead of it was closed).
    const affectsThisPage =
      event.id === incidentId ||
      (Array.isArray(event.affected_ids) && event.affected_ids.includes(incidentId));
    if (affectsThisPage) {
      handleConflict();
    } else if (event.type === "fault" && event.action !== "commented") {
      // Another fault was created / claimed / closed / reopened /
      // reclassified: the number of higher-priority faults ahead of THIS
      // one (if it's an open fault) may have changed. Comments never
      // affect priority, so they're skipped.
      refreshClaimState();
    }
  });
  // If another tab of this browser changes the saved Updates order, follow it
  // right away (in place - nothing typed on this page is lost).
  onUpdatesOrderChanged(applyUpdatesOrder);
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

  // An open fault needs to know whether it is currently claimable. If the
  // lookup fails, claimStatus stays null and the Claim button is rendered
  // disabled with an explanatory tooltip rather than guessing.
  let claimStatus = null;
  if (incident.type === "fault" && incident.status === "open") {
    try {
      claimStatus = await api.faultClaimStatus(incident.id);
    } catch {
      claimStatus = null;
    }
  }

  currentIncident = incident;
  render(incident, claimStatus);
}

// --- Updates list: markup, and in-place re-ordering ---

/**
 * The HTML for the comment list, in the given order ("asc" = oldest first,
 * "desc" = newest first). Used both by the full render() and by
 * applyUpdatesOrder(), so the two can never disagree about how a comment looks.
 */
function commentsListHtml(comments, order) {
  return sortComments(comments, order).map((c) => `
    <div class="comment">
      <span class="comment-author">${escapeHtml(c.author)}</span>
      <span class="comment-time">${formatDate(c.created_at)}</span>
      <p>${escapeHtml(c.text)}</p>
    </div>`).join("") || `<p class="empty">No updates yet.</p>`;
}

/**
 * Applies `order` to the Updates section IN PLACE: rebuilds only the
 * comment list (from the last loaded incident) and syncs the dropdown. The
 * "Add an update" box sits above the list in BOTH orders, so it is never
 * moved - and nothing else on the page is touched, so a half-typed comment
 * is kept. It also works on a page that has been locked as outdated,
 * because ordering is only a view preference, not an action.
 */
function applyUpdatesOrder(order) {
  const list = document.querySelector(".comments-list");
  if (!list || !currentIncident) return;
  list.innerHTML = commentsListHtml(currentIncident.comments, order);
  const select = document.querySelector('[data-role="updates-order"]');
  if (select) select.value = order;
}

// --- claim control (open faults): tooltip text, markup, and in-place refresh ---

/** Tooltip for the Claim button: empty when claimable, otherwise why not. */
function claimTooltip(status) {
  if (!status) return "Could not check the fault queue right now - refresh the page.";
  if (status.claimable) return "";
  if (!status.queued) return "This fault is not waiting in the queue.";
  const n = status.higher_priority_unclaimed;
  return `Not the highest priority at the moment — ${n} higher-priority fault${n === 1 ? " is" : "s are"} still unclaimed.`;
}

/**
 * The Claim button, wrapped in a span that carries the tooltip: a disabled
 * <button> can swallow hover events, so the title lives on the wrapper.
 */
function claimControlHtml(status) {
  const enabled = Boolean(status && status.claimable) && !locked;
  return `
    <span class="claim-wrap" data-role="claim-wrap" title="${escapeHtml(claimTooltip(status))}">
      <button class="btn btn-primary" data-action="claim" ${enabled ? "" : "disabled"}>Claim</button>
    </span>`;
}

/** Patches the existing Claim control in place (no re-render, so nothing typed elsewhere on the page is lost). */
function applyClaimStatus(status) {
  const wrap = document.querySelector('[data-role="claim-wrap"]');
  if (!wrap) return;
  wrap.title = claimTooltip(status);
  wrap.querySelector("button").disabled = locked || !(status && status.claimable);
}

/**
 * Re-checks claimability and updates the button/tooltip in place. Does
 * nothing unless the page is showing an OPEN fault, and never touches a
 * page that has been locked as outdated (that would re-enable a button
 * lockMutatingControls() deliberately disabled).
 */
async function refreshClaimState() {
  if (locked || !currentIncident) return;
  if (currentIncident.type !== "fault" || currentIncident.status !== "open") return;
  try {
    applyClaimStatus(await api.faultClaimStatus(currentIncident.id));
  } catch {
    // Transient failure: leave the button as it is; the next event re-checks.
  }
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

/**
 * Disables every control that could submit a change. Controls marked
 * data-keep-enabled are skipped: those only change how the page is
 * DISPLAYED (currently just the Updates order dropdown), which is safe -
 * and useful - on an outdated page.
 */
function lockMutatingControls() {
  locked = true;
  root.querySelectorAll("button, select, textarea, input").forEach((el) => {
    if (!el.hasAttribute("data-keep-enabled")) el.disabled = true;
  });
}

function handleConflict() {
  if (locked) return; // already declined once for this incident - banner already says so
  const dialog = ensureConflictDialog();
  if (!dialog.open) dialog.showModal();
}

function render(incident, claimStatus) {
  const isFault = incident.type === "fault";
  const isAdmin = user.role === "admin";
  const updatesOrder = getUpdatesOrder();

  const badge = isFault
    ? `<span class="detail-badge ${severityClass(incident.severity)}">${severityLabel(incident.severity)}</span>`
    : `<span class="detail-badge">Maintenance${incident.queue_position ? ` · position ${incident.queue_position}` : ""}</span>`;

  // The buttons that close a fault. Shown both while the fault is still
  // waiting unclaimed in the queue and while it is in progress. Resolve is
  // available to everyone; the two admin-only resolution types are only
  // offered to admins (the server enforces this again regardless).
  const faultCloseButtons = `
      <button class="btn btn-primary" data-action="resolve">Resolve</button>
      ${isAdmin ? `<button class="btn btn-ghost" data-action="not-incident">Not an incident</button>` : ""}
      ${isAdmin ? `<button class="btn btn-ghost" data-action="by-design">By design</button>` : ""}`;

  // The same idea for a maintenance task, whether it is pending anywhere in
  // the queue or in progress. The first button differs only in its label,
  // data-action and style ("Complete" for the in-progress task, "Close task"
  // for a pending one - shown as a secondary button next to Start on the
  // first pending task); all of them end up in the same close dialog.
  const maintenanceCloseButtons = (primaryLabel, primaryAction, primaryClass = "btn-primary") => `
      <button class="btn ${primaryClass}" data-action="${primaryAction}">${primaryLabel}</button>
      ${isAdmin ? `<button class="btn btn-ghost" data-action="not-incident">Not an incident</button>` : ""}
      ${isAdmin ? `<button class="btn btn-ghost" data-action="by-design">By design</button>` : ""}`;

  // Reopening a closed incident is admin-only, for both types. A regular
  // user sees a hint instead of the button (the server rejects them with 403
  // regardless). A fault and a maintenance task use different dialogs: the
  // fault one picks only a status, the maintenance one also picks the
  // position in the FIFO queue.
  const canReopen = isFault && isAdmin && incident.status === "closed";
  const canReopenTask = !isFault && isAdmin && incident.status === "closed";
  let reopenControl = "";
  if (incident.status === "closed") {
    reopenControl = isAdmin
      ? `<button class="btn btn-ghost" data-action="${isFault ? "reopen" : "reopen-task"}">Reopen…</button>`
      : `<p class="hint" style="margin:0">Only an admin can reopen a closed incident.</p>`;
  }

  let actionsHtml;
  if (incident.status === "closed") {
    actionsHtml = `
      <div class="resolution-panel" style="flex-basis:100%;margin-bottom:0">
        <strong>${incident.resolution_type.replace("_", " ")}</strong>
        <p>${escapeHtml(incident.resolution_message)}</p>
      </div>
      ${reopenControl}`;
  } else if (!isFault) {
    if (incident.status === "in_progress") {
      actionsHtml = maintenanceCloseButtons("Complete", "complete");
    } else if (incident.queue_position === 1) {
      // A pending task at position 1 is the head of the queue with nothing
      // in progress (an in-progress task would hold position 1 itself), so
      // it is the one task that may be started right now.
      actionsHtml = `
      <p class="hint" style="flex-basis:100%;margin:0">Next in the maintenance queue — start it now, or close it without starting it.</p>
      <button class="btn btn-primary" data-action="start">Start</button>
      ${maintenanceCloseButtons("Close task", "resolve", "btn-ghost")}`;
    } else {
      actionsHtml = `
      <p class="hint" style="flex-basis:100%;margin:0">Waiting in the maintenance queue — tasks start strictly in order. You can close it here without starting it; the tasks behind it move up.</p>
      ${maintenanceCloseButtons("Close task", "resolve")}`;
    }
  } else if (incident.status !== "in_progress") {
    actionsHtml = `
      <p class="hint" style="flex-basis:100%;margin:0">Waiting in the fault queue, unclaimed. Claim it here when it is the highest priority, or close it without claiming.</p>
      ${claimControlHtml(claimStatus)}
      ${faultCloseButtons}`;
  } else {
    actionsHtml = `
      ${faultCloseButtons}
      ${isAdmin ? `
        <label class="inline-select">Severity
          <select data-role="severity-select">
            <option value="1" ${incident.severity === 1 ? "selected" : ""}>Critical</option>
            <option value="2" ${incident.severity === 2 ? "selected" : ""}>Major</option>
            <option value="3" ${incident.severity === 3 ? "selected" : ""}>Minor</option>
          </select>
        </label>` : ""}`;
  }

  // Only rendered when the matching Reopen button exists (closed + admin).
  const reopenDialogHtml = canReopen ? `
    <dialog id="reopen-dialog" class="dialog">
      <form id="reopen-form" class="dialog-form">
        <h2>Reopen fault</h2>
        <label>Reopen as
          <select name="status">
            <option value="open">Open — back into the fault queue</option>
            <option value="in_progress">In progress — assigned to you</option>
          </select>
        </label>
        <label>Reason<textarea name="reason" rows="3" required placeholder="Why is this fault being reopened?"></textarea></label>
        <p class="form-error" id="reopen-error" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary">Reopen</button>
        </div>
      </form>
    </dialog>` : "";

  // The maintenance reopen popup. Its position/status choices are filled in
  // by wireActions() from GET /incidents/maintenance/reopen-options when it
  // is opened, since what is valid depends on the queue at that moment.
  const reopenTaskDialogHtml = canReopenTask ? `
    <dialog id="reopen-task-dialog" class="dialog">
      <form id="reopen-task-form" class="dialog-form">
        <h2>Reopen task</h2>
        <p class="hint" id="reopen-task-note" style="margin:0 0 14px"></p>
        <div id="reopen-task-placement">
          <label>Position in the queue
            <select name="placement">
              <option value="start">Start</option>
              <option value="end" selected>End</option>
              <option value="number">A specific position…</option>
            </select>
          </label>
          <label id="reopen-task-number-label" hidden>Position number
            <input type="number" name="position" step="1">
          </label>
        </div>
        <label>Reopen as
          <select name="status">
            <option value="open">Open — waiting in the queue</option>
            <option value="in_progress">In progress — only at position 1</option>
          </select>
        </label>
        <label>Reason<textarea name="reason" rows="3" required placeholder="Why is this task being reopened?"></textarea></label>
        <p class="form-error" id="reopen-task-error" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary">Reopen</button>
        </div>
      </form>
    </dialog>` : "";

  // Updates section: header (with the order dropdown), then the "Add an
  // update" box, then the list. The box is ALWAYS directly under the header,
  // above the list, whichever order is chosen - the order only affects the
  // list below it.
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
        <div class="comments-header">
          <h2>Updates</h2>
          <label class="inline-select">Order
            <select data-role="updates-order" data-keep-enabled>
              <option value="asc" ${updatesOrder === "asc" ? "selected" : ""}>Oldest first</option>
              <option value="desc" ${updatesOrder === "desc" ? "selected" : ""}>Newest first</option>
            </select>
          </label>
        </div>
        <form class="comment-form" id="comment-form">
          <textarea name="text" rows="2" placeholder="Add an update…" required></textarea>
          <button type="submit" class="btn btn-primary">Post</button>
        </form>
        <div class="comments-list">${commentsListHtml(incident.comments, updatesOrder)}</div>
      </section>
    </article>

    <dialog id="close-dialog" class="dialog">
      <form id="close-form" class="dialog-form">
        <h2 id="close-dialog-title">Resolve incident</h2>
        <label>Reason<textarea name="message" rows="3" required placeholder="Explain what happened and why you are closing this…"></textarea></label>
        <p class="form-error" id="close-error" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary">Confirm</button>
        </div>
      </form>
    </dialog>
    ${reopenDialogHtml}
    ${reopenTaskDialogHtml}
  `;

  wireActions(incident, isFault);
}

function wireActions(incident, isFault) {
  // Updates order dropdown. Deliberately NOT guarded by `locked`: it only
  // changes how the page is displayed, never submits anything, so it keeps
  // working on a page that has been locked as outdated. Saving the choice
  // lets every other page (and other tabs, via the storage event) follow it.
  const orderSelect = document.querySelector('[data-role="updates-order"]');
  orderSelect.addEventListener("change", () => {
    setUpdatesOrder(orderSelect.value);
    applyUpdatesOrder(orderSelect.value);
  });

  document.getElementById("comment-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (locked) return;
    const text = e.target.text.value.trim();
    if (!text) return;
    await api.addComment(incident.id, text);
    await load();
  });

  // Start button (only on the first pending maintenance task). The server
  // starts exactly THIS task and only if it is still first in line with
  // nothing in progress, so if the queue changed since this page loaded the
  // request is rejected with a message instead of starting another task.
  const startBtn = document.querySelector('[data-action="start"]');
  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      if (locked) return;
      startBtn.disabled = true; // guard against a double click while the request is in flight
      try {
        await api.startMaintenanceTask(incident.id);
        await load();
      } catch (err) {
        alert(err.message);
        startBtn.disabled = false;
      }
    });
  }

  // Claim button (open faults only). The server re-checks the priority
  // rule, so if the situation changed since the button was last updated
  // the claim is rejected with a message; we then show it and refresh the
  // button/tooltip to match reality.
  const claimBtn = document.querySelector('[data-action="claim"]');
  if (claimBtn) {
    claimBtn.addEventListener("click", async () => {
      if (locked) return;
      claimBtn.disabled = true; // guard against a double click while the request is in flight
      try {
        await api.claimFault(incident.id);
        await load();
      } catch (err) {
        alert(err.message);
        await refreshClaimState();
      }
    });
  }

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
  bind('[data-action="resolve"]', "resolved", isFault ? "Resolve fault" : "Close task");
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
        // Every maintenance close - the in-progress task or a pending one
        // from anywhere in the queue - goes through the same endpoint.
        await api.closeMaintenanceTask(incident.id, message, pendingResolutionType);
      }
      closeDialog.close();
      await load();
    } catch (err) {
      closeError.textContent = err.message;
      closeError.hidden = false;
    }
  });

  // Fault reopen dialog: only present in the DOM for a closed fault viewed
  // by an admin (see render()), so everything here is skipped otherwise.
  const reopenDialog = document.getElementById("reopen-dialog");
  if (reopenDialog) {
    const reopenForm = document.getElementById("reopen-form");
    const reopenError = document.getElementById("reopen-error");

    document.querySelector('[data-action="reopen"]').addEventListener("click", () => {
      if (locked) return;
      reopenError.hidden = true;
      reopenForm.reset();
      reopenDialog.showModal();
    });
    reopenDialog.querySelector('[data-action="cancel"]').addEventListener("click", () => reopenDialog.close());

    reopenForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (locked) return;
      reopenError.hidden = true;
      const status = reopenForm.elements["status"].value;
      const reason = reopenForm.elements["reason"].value.trim();
      try {
        await api.reopenFault(incident.id, status, reason);
        reopenDialog.close();
        await load();
      } catch (err) {
        reopenError.textContent = err.message;
        reopenError.hidden = false;
      }
    });
  }

  // Maintenance reopen dialog: only present for a closed maintenance task
  // viewed by an admin. When opened it asks the server what the queue looks
  // like right now (GET /incidents/maintenance/reopen-options) and offers
  // only valid choices:
  //   - the position controls are hidden when the queue is empty (the task
  //     just becomes position 1);
  //   - "Start" = the first allowed position, "End" = the last, or a
  //     specific number within that range;
  //   - "In progress" is only enabled when the chosen position is 1 AND
  //     nothing is currently in progress.
  // The server re-validates on submit, so a queue that changed meanwhile
  // simply produces an error message here instead of a wrong placement.
  const reopenTaskDialog = document.getElementById("reopen-task-dialog");
  if (reopenTaskDialog) {
    const form = document.getElementById("reopen-task-form");
    const errorEl = document.getElementById("reopen-task-error");
    const noteEl = document.getElementById("reopen-task-note");
    const placementEl = document.getElementById("reopen-task-placement");
    const numberLabel = document.getElementById("reopen-task-number-label");
    const placementSelect = form.elements["placement"];
    const numberInput = form.elements["position"];
    const statusSelect = form.elements["status"];
    let options = null; // the latest reopen-options response

    const chosenPosition = () => {
      const mode = placementSelect.value;
      if (mode === "start") return options.first_position;
      if (mode === "end") return options.last_position;
      return Number(numberInput.value);
    };

    // Re-derives what is allowed from the current choices.
    const syncForm = () => {
      if (!options) return;
      numberLabel.hidden = placementSelect.value !== "number";
      const inProgressAllowed = options.can_start && chosenPosition() === 1;
      statusSelect.querySelector('option[value="in_progress"]').disabled = !inProgressAllowed;
      if (!inProgressAllowed && statusSelect.value === "in_progress") statusSelect.value = "open";
    };
    placementSelect.addEventListener("change", syncForm);
    numberInput.addEventListener("input", syncForm);

    document.querySelector('[data-action="reopen-task"]').addEventListener("click", async () => {
      if (locked) return;
      errorEl.hidden = true;
      form.reset();
      try {
        options = await api.maintenanceReopenOptions();
      } catch (err) {
        alert(err.message);
        return;
      }
      placementEl.hidden = options.queue_empty;
      numberInput.min = options.first_position;
      numberInput.max = options.last_position;
      numberInput.value = options.last_position;
      if (options.queue_empty) {
        noteEl.textContent = "The queue is empty - the task will be placed at position 1.";
      } else if (options.has_current) {
        noteEl.textContent = `A task is in progress and holds position 1, so this task can go at positions ${options.first_position} to ${options.last_position}, and cannot be put in progress.`;
      } else {
        noteEl.textContent = `This task can go at positions ${options.first_position} to ${options.last_position}. Only position 1 can be put in progress.`;
      }
      syncForm();
      reopenTaskDialog.showModal();
    });
    reopenTaskDialog.querySelector('[data-action="cancel"]').addEventListener("click", () => reopenTaskDialog.close());

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (locked || !options) return;
      errorEl.hidden = true;
      const position = chosenPosition();
      if (!Number.isInteger(position)) {
        errorEl.textContent = "Enter a whole position number.";
        errorEl.hidden = false;
        return;
      }
      try {
        await api.reopenMaintenanceTask(incident.id, statusSelect.value, form.elements["reason"].value.trim(), position);
        reopenTaskDialog.close();
        await load();
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.hidden = false;
      }
    });
  }

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
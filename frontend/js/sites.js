/**
 * Site portal page (sites.html) - admin only.
 *
 * Loads sites, mailing lists, notifications and the site health report from
 * the /sites API (backend/app/api/sites.py) and renders:
 *   - an attention sentence, a status board (one chip per active site, most
 *     urgent first) and the report's findings;
 *   - four tabs: Sites, Mailing lists, Notifications, Archive.
 * Every action (check, status change, create/edit, archive/restore,
 * send/dismiss) calls the API and then reloads everything; rows are patched
 * in place by row-list.js, so reloads don't flicker.
 *
 * Non-admins are sent back to the dashboard (the API would answer 403 to
 * every call anyway). Live updates: listens to "sites" events (only ever sent
 * to admin tabs) and "incidents" events (incident mentions change the
 * counts), shows a toast for site events, and reloads.
 *
 * Wording and HTML-escaping helpers live in sites-format.js.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { connectLiveUpdates } from "./events.js";
import { renderRowList } from "./row-list.js";
import { showToast } from "./toast.js";
import {
  attentionSentence, describeOwnCheck, describeSiteEvent, escapeHtml, formatDate, formatDateTime,
  label, plural, reportFindings, siteLabels,
} from "./sites-format.js";

const $ = (id) => document.getElementById(id);

// Latest data from the server - dialogs read from here.
let sites = [];          // every site, archived included
let lists = [];          // every mailing list, archived included
let notifications = [];  // filtered by the Notifications tab's selector
let editingSiteId = null;
let editingListId = null;
let statusSiteId = null;
let reviewing = null;    // the notification open in the review dialog

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  $("portal").hidden = false;
  wireTabs();
  wireActions();
  wireDialogs();
  await refresh();
  connectLiveUpdates(onLiveEvent, { scopes: ["sites", "incidents"] });
}

// ---------------------------------------------------------------------------
// Loading and rendering
// ---------------------------------------------------------------------------

async function refresh() {
  try {
    const [allSites, allLists, report, filtered] = await Promise.all([
      api.listSites(true), api.listMailingLists(true), api.siteReport(),
      api.listNotifications($("notification-filter").value),
    ]);
    sites = allSites;
    lists = allLists;
    notifications = filtered;
    renderReport(report);
    renderSites();
    renderLists();
    renderNotifications();
    renderArchive();
  } catch (err) {
    $("attention").textContent = err.message;
  }
}

let refreshTimer = null;
/** Coalesce bursts of live-update events into one reload. */
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 250);
}

function onLiveEvent(event) {
  if (event.scope === "sites") showToast(describeSiteEvent(event));
  scheduleRefresh();
}

function renderReport(report) {
  $("attention").innerHTML = attentionSentence(report);
  $("status-board").innerHTML = report.rows.map((row) => `
    <button class="site-chip status-${row.status}" data-action="details" data-site-id="${row.site_id}">
      <span class="site-chip-label">Site ${row.site_id}</span>
      <span class="site-chip-name">${escapeHtml(row.site_name)}</span>
      <span class="site-chip-status status-text status-${row.status}">${label(row.status)}</span>
    </button>`).join("");
  const findings = reportFindings(report);
  $("report-checks").innerHTML = findings.length
    ? findings.map((f) => `<li>${escapeHtml(f.text)}${f.tab
      ? ` <button class="btn btn-ghost btn-sm" data-action="open-tab" data-tab="${f.tab}">Open</button>` : ""}</li>`).join("")
    : `<li class="hint">No problems found.</li>`;
  const drafts = report.notification_counts.draft || 0;
  $("draft-count").textContent = drafts ? `(${drafts})` : "";
}

function siteActions(site) {
  return `
    <span class="row-actions">
      <button class="btn btn-ghost btn-sm" data-action="check" data-site-id="${site.site_id}"
        ${site.checking ? "disabled" : ""}>${site.checking ? "Checking…" : "Check"}</button>
      <button class="btn btn-ghost btn-sm" data-action="status" data-site-id="${site.site_id}">Status</button>
      <button class="btn btn-ghost btn-sm" data-action="edit-site" data-site-id="${site.site_id}">Edit</button>
      <button class="btn btn-ghost btn-sm" data-action="archive-site" data-site-id="${site.site_id}">Archive</button>
    </span>`;
}

function renderSites() {
  const active = sites.filter((s) => !s.is_archived);
  renderRowList($("site-list"), active, (s) => s.site_id, (s) => {
    const check = s.last_check ? `Last check ${formatDateTime(s.last_check.checked_at)}: ${s.last_check.summary}` : "Never checked";
    const html = `
      <span class="status-text status-${s.status}">${label(s.status)}</span>
      <span class="row-main">
        <span class="row-title">${escapeHtml(s.site_name)}</span>
        <span class="row-sub">${s.label}, ${escapeHtml(s.site_url)}</span>
        <span class="row-sub">${escapeHtml(check)}</span>
      </span>
      <span class="row-meta">${plural(s.incident_ids.length, "active incident")}</span>
      ${siteActions(s)}`;
    return { className: `row status-${s.status}`, html, sig: JSON.stringify(s) };
  }, "No active sites. Add one with New site.");
}

function renderLists() {
  const active = lists.filter((l) => !l.is_archived);
  renderRowList($("list-list"), active, (l) => l.list_id, (l) => ({
    className: "row",
    sig: JSON.stringify(l),
    html: `
      <span class="row-main">
        <span class="row-title">${escapeHtml(l.name)}</span>
        <span class="row-sub">${escapeHtml(l.list_id)}, ${plural(l.member_count, "member")}</span>
        <span class="row-sub">${l.site_ids.length ? siteLabels(l.site_ids) : "No sites"}</span>
      </span>
      <span class="row-actions">
        <button class="btn btn-ghost btn-sm" data-action="edit-list" data-list-id="${escapeHtml(l.list_id)}">Edit</button>
        <button class="btn btn-ghost btn-sm" data-action="archive-list" data-list-id="${escapeHtml(l.list_id)}">Archive</button>
      </span>`,
  }), "No mailing lists. Create one with New mailing list.");
}

function renderNotifications() {
  const empty = $("notification-filter").value === "draft" ? "No notifications are waiting for a decision." : "No notifications.";
  renderRowList($("notification-list"), notifications, (n) => n.id, (n) => ({
    // Colored by the NOTIFICATION's state (draft = needs a decision, sent =
    // done, dismissed/skipped = no action), not by the site's status - the
    // site's old and new status are in the title text.
    className: `row notification-${n.state}`,
    sig: JSON.stringify(n),
    html: `
      <span class="status-text notification-${n.state}">${label(n.state)}</span>
      <span class="row-main">
        <span class="row-title">Site ${n.site_id} (${escapeHtml(n.site_name)}): ${label(n.old_status)} to ${label(n.new_status)}</span>
        <span class="row-sub">${plural(n.recipient_count, "recipient")}, created ${formatDateTime(n.created_at)} by ${escapeHtml(n.created_by)}</span>
      </span>
      <span class="row-actions">
        <button class="btn ${n.state === "draft" ? "btn-primary" : "btn-ghost"} btn-sm" data-action="review" data-notification-id="${n.id}">
          ${n.state === "draft" ? "Review" : "View"}</button>
      </span>`,
  }), empty);
}

function renderArchive() {
  renderRowList($("archived-site-list"), sites.filter((s) => s.is_archived), (s) => s.site_id, (s) => ({
    className: "row",
    sig: JSON.stringify(s),
    html: `
      <span class="row-main">
        <span class="row-title">${escapeHtml(s.site_name)}</span>
        <span class="row-sub">${s.label}, ${escapeHtml(s.site_url)}, archived ${formatDateTime(s.archived_at)}</span>
      </span>
      <span class="row-actions"><button class="btn btn-ghost btn-sm" data-action="restore-site" data-site-id="${s.site_id}">Restore</button></span>`,
  }), "No archived sites.");
  renderRowList($("archived-list-list"), lists.filter((l) => l.is_archived), (l) => l.list_id, (l) => ({
    className: "row",
    sig: JSON.stringify(l),
    html: `
      <span class="row-main">
        <span class="row-title">${escapeHtml(l.name)}</span>
        <span class="row-sub">${escapeHtml(l.list_id)}, archived ${formatDateTime(l.archived_at)}</span>
      </span>
      <span class="row-actions"><button class="btn btn-ghost btn-sm" data-action="restore-list" data-list-id="${escapeHtml(l.list_id)}">Restore</button></span>`,
  }), "No archived mailing lists.");
}

// ---------------------------------------------------------------------------
// Tabs and actions
// ---------------------------------------------------------------------------

function showTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const selected = tab.id === `tab-${name}`;
    tab.setAttribute("aria-selected", String(selected));
    $(tab.getAttribute("aria-controls")).hidden = !selected;
  });
}

function wireTabs() {
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => showTab(tab.id.replace("tab-", ""))));
  $("notification-filter").addEventListener("change", refresh);
}

/** Run an API call from a button, keeping the button disabled meanwhile. Errors are shown in an alert. */
async function busy(button, work) {
  if (button) button.disabled = true;
  try {
    await work();
  } catch (err) {
    alert(err.message);
  } finally {
    if (button) button.disabled = false;
  }
}

function wireActions() {
  const checkAllButton = $("check-all-btn");
  checkAllButton.addEventListener("click", () => busy(checkAllButton, async () => {
    checkAllButton.textContent = "Checking…";
    try {
      const result = await api.checkAllSites();
      const changed = result.results.filter((r) => r.change).length;
      showToast(`Checked ${plural(result.checked, "site")}, ${changed} changed status`);
    } finally {
      checkAllButton.textContent = "Check all sites";
    }
    await refresh();
  }));
  $("new-site-btn").addEventListener("click", () => openSiteDialog(null));
  $("new-list-btn").addEventListener("click", () => openListDialog(null));

  // One delegated listener for every row button and status-board chip.
  $("portal").addEventListener("click", (e) => {
    const target = e.target.closest("[data-action]");
    if (!target) {
      const row = e.target.closest("#site-list .row[data-key]");
      if (row) openDetails(Number(row.dataset.key));
      return;
    }
    const siteId = Number(target.dataset.siteId);
    const listId = target.dataset.listId;
    switch (target.dataset.action) {
      case "details": return openDetails(siteId);
      case "open-tab": return showTab(target.dataset.tab);
      case "check":
        return busy(target, async () => { showToast(describeOwnCheck(await api.checkSite(siteId))); await refresh(); });
      case "status": return openStatusDialog(siteId);
      case "edit-site": return openSiteDialog(siteId);
      case "archive-site":
        if (!confirm(`Archive Site ${siteId}? It will be removed from its mailing lists and no longer checked. You can restore it from the Archive tab.`)) return;
        return busy(target, async () => { await api.archiveSite(siteId); await refresh(); });
      case "restore-site": return busy(target, async () => { await api.restoreSite(siteId); await refresh(); });
      case "edit-list": return openListDialog(listId);
      case "archive-list":
        if (!confirm(`Archive mailing list ${listId}? It will receive no notifications until restored.`)) return;
        return busy(target, async () => { await api.archiveMailingList(listId); await refresh(); });
      case "restore-list": return busy(target, async () => { await api.restoreMailingList(listId); await refresh(); });
      case "review": return openReview(target.dataset.notificationId);
      default: return undefined;
    }
  });
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

function showError(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function wireDialogs() {
  document.querySelectorAll("dialog [data-action='cancel']").forEach((btn) =>
    btn.addEventListener("click", () => btn.closest("dialog").close()));
  $("site-form").addEventListener("submit", submitSite);
  $("status-form").addEventListener("submit", submitStatus);
  $("list-form").addEventListener("submit", submitList);
  $("notification-form").addEventListener("submit", sendReviewed);
  $("notification-dismiss").addEventListener("click", dismissReviewed);
}

function openSiteDialog(siteId) {
  editingSiteId = siteId;
  const form = $("site-form");
  form.reset();
  showError("site-error", "");
  const site = sites.find((s) => s.site_id === siteId);
  $("site-dialog-title").textContent = site ? `Edit ${site.label}` : "New site";
  $("site-submit").textContent = site ? "Save changes" : "Create site";
  if (site) {
    form.site_name.value = site.site_name;
    form.site_url.value = site.site_url;
    form.site_publish_date.value = site.site_publish_date || "";
  }
  $("site-dialog").showModal();
}

async function submitSite(e) {
  e.preventDefault();
  const form = e.target;
  const fields = {
    site_name: form.site_name.value.trim(),
    site_url: form.site_url.value.trim(),
    site_publish_date: form.site_publish_date.value || null,
  };
  try {
    if (editingSiteId === null) {
      const created = await api.createSite(fields);
      showToast(`Added ${created.label}`);
    } else {
      const site = sites.find((s) => s.site_id === editingSiteId);
      const changes = Object.fromEntries(Object.entries(fields).filter(([key, value]) => value !== (site[key] ?? null)));
      if (Object.keys(changes).length) await api.updateSite(editingSiteId, changes);
    }
    $("site-dialog").close();
    await refresh();
  } catch (err) {
    showError("site-error", err.message);
  }
}

function openStatusDialog(siteId) {
  statusSiteId = siteId;
  const site = sites.find((s) => s.site_id === siteId);
  const form = $("status-form");
  form.reset();
  showError("status-error", "");
  $("status-dialog-title").textContent = `Change status of ${site.label}`;
  if (site.status !== "unknown") form.status.value = site.status;
  $("status-dialog").showModal();
}

async function submitStatus(e) {
  e.preventDefault();
  const form = e.target;
  try {
    const result = await api.changeSiteStatus(statusSiteId, form.status.value, form.reason.value);
    $("status-dialog").close();
    if (!result.change) showToast(`Site ${statusSiteId} already had that status - nothing changed.`);
    else if (result.notification && result.notification.state === "draft") {
      showToast("Status changed. A notification is waiting for a decision.");
    }
    await refresh();
  } catch (err) {
    showError("status-error", err.message);
  }
}

function openListDialog(listId) {
  editingListId = listId;
  const form = $("list-form");
  form.reset();
  showError("list-error", "");
  const list = lists.find((l) => l.list_id === listId);
  $("list-dialog-title").textContent = list ? `Edit ${list.name}` : "New mailing list";
  $("list-submit").textContent = list ? "Save changes" : "Create list";
  $("list-id-label").hidden = Boolean(list);
  form.list_id.required = !list;
  const linked = new Set(list ? list.site_ids : []);
  $("list-site-choices").innerHTML = sites.filter((s) => !s.is_archived).map((s) => `
    <label class="checkbox"><input type="checkbox" name="site" value="${s.site_id}" ${linked.has(s.site_id) ? "checked" : ""}>
      ${s.label} (${escapeHtml(s.site_name)})</label>`).join("") || `<p class="hint">No active sites yet.</p>`;
  if (list) {
    form.name.value = list.name;
    form.members.value = list.members.join("\n");
  }
  $("list-dialog").showModal();
}

async function submitList(e) {
  e.preventDefault();
  const form = e.target;
  const members = form.members.value.split(/[\n,]/).map((m) => m.trim()).filter(Boolean);
  const siteIds = [...form.querySelectorAll("input[name='site']:checked")].map((box) => Number(box.value));
  try {
    if (editingListId === null) {
      await api.createMailingList({ list_id: form.list_id.value.trim(), name: form.name.value.trim(), members, site_ids: siteIds });
    } else {
      await api.updateMailingList(editingListId, { name: form.name.value.trim(), members, site_ids: siteIds });
    }
    $("list-dialog").close();
    await refresh();
  } catch (err) {
    showError("list-error", err.message);
  }
}

async function openReview(notificationId) {
  reviewing = notifications.find((n) => n.id === notificationId);
  if (!reviewing) return;
  const n = reviewing;
  const form = $("notification-form");
  const isDraft = n.state === "draft";
  form.reset();
  showError("notification-error", "");
  $("notification-dialog-title").textContent = `Site ${n.site_id} (${n.site_name}): ${label(n.old_status)} to ${label(n.new_status)}`;
  $("notification-meta").textContent = isDraft
    ? `Draft created ${formatDateTime(n.created_at)} by ${n.created_by}`
    : `${label(n.state)} ${formatDateTime(n.decided_at)} by ${n.decided_by || n.created_by}`
      + (n.dismiss_reason ? `. Reason: ${n.dismiss_reason}` : "");
  $("notification-recipients").textContent = n.recipient_count
    ? `To ${n.list_ids.join(", ")}: ${n.recipients.join(", ")}`
    : "No mailing list with members covers this site, so there is nobody to send it to.";
  form.message.value = n.message;
  form.message.readOnly = !isDraft;
  form.reason.closest("label").hidden = !isDraft;
  $("notification-send").hidden = !isDraft;
  $("notification-dismiss").hidden = !isDraft;
  $("notification-dialog").showModal();
}

async function sendReviewed(e) {
  e.preventDefault();
  const message = $("notification-form").message.value.trim();
  try {
    await api.sendNotification(reviewing.id, message !== reviewing.message ? message : null);
    $("notification-dialog").close();
    showToast(`Sent to ${plural(reviewing.recipient_count, "recipient")}`);
    await refresh();
  } catch (err) {
    showError("notification-error", err.message);
  }
}

async function dismissReviewed() {
  try {
    await api.dismissNotification(reviewing.id, $("notification-form").reason.value.trim() || null);
    $("notification-dialog").close();
    await refresh();
  } catch (err) {
    showError("notification-error", err.message);
  }
}

async function openDetails(siteId) {
  let site;
  try {
    site = await api.getSite(siteId);
  } catch (err) {
    alert(err.message);
    return;
  }
  $("detail-title").textContent = `${site.label}: ${site.site_name}`;
  $("detail-meta").innerHTML = `<a href="${escapeHtml(site.site_url)}" target="_blank" rel="noopener">${escapeHtml(site.site_url)}</a>`
    + (site.site_publish_date ? `, published ${formatDate(site.site_publish_date)}` : "")
    + `, <span class="status-text status-${site.status}">${label(site.status)}</span>`;
  const check = site.last_check
    ? `${formatDateTime(site.last_check.checked_at)}: ${escapeHtml(site.last_check.summary)}`
      + (site.consecutive_failures ? ` (${plural(site.consecutive_failures, "failure")} in a row)` : "")
    : "Never checked";
  const incidents = site.incidents.length
    ? site.incidents.map((i) => `<li><a href="/incident.html?id=${i.id}">${escapeHtml(i.title)}</a>
        <span class="hint">${label(i.type)}, ${label(i.status)}</span></li>`).join("")
    : `<li class="hint">No active incident mentions ${site.label}.</li>`;
  const history = site.status_history.length
    ? [...site.status_history].reverse().map((c) => `<li>${formatDateTime(c.changed_at)}: ${label(c.old_status)} to
        ${label(c.new_status)} by ${escapeHtml(c.actor)}<br><span class="hint">${escapeHtml(c.reason)}</span></li>`).join("")
    : `<li class="hint">No status changes yet.</li>`;
  const sent = site.notifications.length
    ? site.notifications.map((n) => `<li>${label(n.state)}: ${label(n.old_status)} to ${label(n.new_status)},
        ${formatDateTime(n.created_at)}</li>`).join("")
    : `<li class="hint">No notifications yet.</li>`;
  $("detail-body").innerHTML = `
    <div class="detail-section"><h3>Last check</h3><p>${check}</p></div>
    <div class="detail-section"><h3>Mailing lists</h3>
      <p>${site.list_ids.length ? escapeHtml(site.list_ids.join(", ")) : "No mailing list covers this site."}</p></div>
    <div class="detail-section"><h3>Active incidents</h3><ul class="detail-list">${incidents}</ul></div>
    <div class="detail-section"><h3>Status history</h3><ul class="detail-list">${history}</ul></div>
    <div class="detail-section"><h3>Notifications</h3><ul class="detail-list">${sent}</ul></div>`;
  $("detail-dialog").showModal();
}
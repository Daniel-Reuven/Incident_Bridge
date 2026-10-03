/**
 * Formatting helpers for the site portal (sites.html / sites.js).
 *
 * Pure functions only - no DOM access, no API calls - so the wording of the
 * portal lives in one place and sites.js stays about behavior. Every value
 * that came from the server (names, URLs, messages) must go through
 * escapeHtml() before it is put into innerHTML.
 */

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** "in_progress" -> "In progress", "down" -> "Down". */
export function label(value) {
  if (!value) return "";
  const text = String(value).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function formatDateTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "never";
}

export function formatDate(iso) {
  return iso ? new Date(`${iso}T00:00:00`).toLocaleDateString() : "";
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** "Site 1042, Site 1043" from a list of ids. */
export function siteLabels(ids) {
  return ids.map((id) => `Site ${id}`).join(", ");
}

/** The one-sentence summary above the status board, from the report (GET /sites/report). */
export function attentionSentence(report) {
  const urgent = report.rows.find((row) => row.site_id === report.most_urgent);
  if (urgent) {
    const who = urgent.incident_count
      ? `${plural(urgent.incident_count, "active incident")} mention it.`
      : "No incident mentions it yet.";
    return `<strong>Site ${urgent.site_id} (${escapeHtml(urgent.site_name)}) is ${urgent.status}.</strong> ${who}`;
  }
  if (report.rows.length === 0) return "There are no active sites. Add one with New site.";
  const unchecked = report.status_counts.unknown || 0;
  return unchecked
    ? `Nothing is down or degraded. ${plural(unchecked, "site")} not checked yet - use Check all sites.`
    : "Nothing is down or degraded.";
}

/**
 * The report's decision-support findings as short sentences - only the ones
 * that found something. Each item: { text, tab } where `tab` (optional) is
 * the portal tab that helps fix it.
 */
export function reportFindings(report) {
  const items = [];
  const add = (ids, text, tab) => { if (ids.length) items.push({ text, tab }); };
  add(report.unreported_outages, `Down or degraded, but no active incident mentions them: ${siteLabels(report.unreported_outages)}`);
  add(report.unflagged_incidents, `Marked operational, but an active incident mentions them: ${siteLabels(report.unflagged_incidents)}`);
  add(report.uncovered_sites, `No mailing list covers them, so nobody would be notified: ${siteLabels(report.uncovered_sites)}`, "lists");
  add(report.lists_without_sites, `Mailing lists with no sites: ${report.lists_without_sites.join(", ")}`, "lists");
  add(report.lists_without_members, `Mailing lists with no members: ${report.lists_without_members.join(", ")}`, "lists");
  const refs = (map) => Object.entries(map).map(([id, incidents]) => `Site ${id} (${plural(incidents.length, "incident")})`);
  const archived = refs(report.archived_references);
  if (archived.length) items.push({ text: `Incidents mention archived sites: ${archived.join(", ")}`, tab: "archive" });
  const unknown = refs(report.unknown_references);
  if (unknown.length) items.push({ text: `Incidents mention sites that do not exist: ${unknown.join(", ")}` });
  const drafts = report.notification_counts.draft || 0;
  if (drafts) items.push({ text: `${plural(drafts, "notification")} waiting for a decision`, tab: "notifications" });
  return items;
}

/**
 * Toast text for a live-update event from another admin (scope "sites" -
 * see backend/app/api/sites.py for the event shape).
 */
export function describeSiteEvent(event) {
  const who = event.actor;
  const what = event.subject;
  const drafted = event.notification === "draft" ? " A notification is waiting for a decision." : "";
  switch (event.action) {
    case "site_created": return `${who} added ${what}`;
    case "site_updated": return `${who} edited ${what}`;
    case "site_status_changed": return `${who} set ${what} to ${label(event.status)}.${drafted}`;
    case "site_checked":
      return `${who} checked ${what}: ${event.changed ? "now" : "still"} ${label(event.status)}.${drafted}`;
    case "sites_checked": return `${who} checked ${plural(event.checked, "site")}, ${event.changed} changed status`;
    case "site_archived": return `${who} archived ${what}`;
    case "site_restored": return `${who} restored ${what}`;
    case "list_created": return `${who} created ${what}`;
    case "list_updated": return `${who} edited ${what}`;
    case "list_archived": return `${who} archived ${what}`;
    case "list_restored": return `${who} restored ${what}`;
    case "notification_sent": return `${who} sent ${what}`;
    case "notification_dismissed": return `${who} dismissed ${what}`;
    default: return `${what} was updated`;
  }
}

/** Toast text for the result of this admin's own check of one site. */
export function describeOwnCheck(result) {
  const site = result.site;
  const state = result.change ? `now ${site.status}` : `still ${site.status}`;
  const drafted = result.notification && result.notification.state === "draft"
    ? " A notification is waiting for a decision." : "";
  return `Site ${site.site_id}: ${result.check.summary}, ${state}.${drafted}`;
}

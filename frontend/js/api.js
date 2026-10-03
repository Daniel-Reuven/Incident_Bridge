/**
 * Thin fetch wrapper around the Incident Bridge API.
 *
 * BASE is empty because the frontend is served by the same FastAPI app
 * as the API (same-origin) - see backend/README.md. If you ever split
 * them onto different origins, set BASE to the API's origin and add it
 * to ALLOWED_ORIGINS on the backend.
 */
import { getClientId } from "./events.js";

const BASE = "";

async function request(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  // Only meaningful on mutating calls - lets the server skip echoing this
  // tab's own change back to itself over the live-update stream (see
  // events.js and backend/app/events.py). Harmless to send on GETs too,
  // but there's nothing for the server to do with it there.
  if (method !== "GET") headers["X-Client-Id"] = getClientId();

  const res = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });

  let data = null;
  try {
    data = await res.json();
  } catch {
    // no JSON body (e.g. some error responses) - fine, data stays null
  }

  if (!res.ok) {
    const message = (data && data.detail) || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return data;
}

export const api = {
  // --- auth ---
  login: (username, password) => request("/auth/login", { method: "POST", body: { username, password } }),
  logout: () => request("/auth/logout", { method: "POST" }),
  me: () => request("/auth/me"),
  changePassword: (current_password, new_password) =>
    request("/auth/change-password", { method: "POST", body: { current_password, new_password } }),

  // --- shared incident operations ---
  listIncidents: (params = {}) => {
    const query = new URLSearchParams(Object.fromEntries(Object.entries(params).filter(([, v]) => v)));
    const qs = query.toString();
    return request("/incidents" + (qs ? `?${qs}` : ""));
  },
  getIncident: (id) => request(`/incidents/${id}`),
  addComment: (id, text) => request(`/incidents/${id}/comments`, { method: "POST", body: { text } }),

  // --- maintenance (strict FIFO) ---
  maintenanceQueue: () => request("/incidents/maintenance/queue"),
  createMaintenanceTask: (title, description) =>
    request("/incidents/maintenance", { method: "POST", body: { title, description } }),
  // Open maintenance calls waiting more than 3 days (lazy pipeline - backend/app/iterators.py).
  // `limit` is optional: omit it to get every pressing call.
  pressingMaintenance: (limit) =>
    request("/incidents/maintenance/pressing" + (limit ? `?limit=${encodeURIComponent(limit)}` : "")),
  // In-progress incidents with no update for a while (generator - backend/app/iterators.py).
  staleIncidents: (minutes) =>
    request("/incidents/work/stale" + (minutes ? `?stale_after_minutes=${encodeURIComponent(minutes)}` : "")),
  startNextMaintenance: () => request("/incidents/maintenance/start-next", { method: "POST" }),
  startMaintenanceTask: (id) => request(`/incidents/maintenance/${id}/start`, { method: "POST" }),
  completeCurrentMaintenance: (message, resolution_type = "resolved") =>
    request("/incidents/maintenance/complete-current", { method: "POST", body: { message, resolution_type } }),
  closeMaintenanceTask: (id, message, resolution_type = "resolved") =>
    request(`/incidents/maintenance/${id}/close`, { method: "POST", body: { message, resolution_type } }),
  maintenanceReopenOptions: () => request("/incidents/maintenance/reopen-options"),
  reopenMaintenanceTask: (id, status, reason, position) =>
    request(`/incidents/maintenance/${id}/reopen`, { method: "POST", body: { status, reason, position } }),

  // --- faults (shared priority queue) ---
  faultQueue: () => request("/incidents/faults/queue"),
  createFault: (title, description, details = {}) =>
    request("/incidents/faults", { method: "POST", body: { title, description, details } }),
  claimNextFault: () => request("/incidents/faults/claim-next", { method: "POST" }),
  faultClaimStatus: (id) => request(`/incidents/faults/${id}/claim-status`),
  claimFault: (id) => request(`/incidents/faults/${id}/claim`, { method: "POST" }),
  changeFaultSeverity: (id, severity) =>
    request(`/incidents/faults/${id}/severity`, { method: "PATCH", body: { severity } }),
  closeFault: (id, resolution_type, message) =>
    request(`/incidents/faults/${id}/close`, { method: "POST", body: { resolution_type, message } }),
  reopenFault: (id, status, reason) =>
    request(`/incidents/faults/${id}/reopen`, { method: "POST", body: { status, reason } }),
  // --- bulk import (admin only) ---
  importJsonl: (content) => request("/incidents/import-jsonl", { method: "POST", body: { content } }),

  // --- site portal (admin only - every call returns 403 for other roles; see backend/app/api/sites.py) ---
  listSites: (includeArchived = false) => request("/sites" + (includeArchived ? "?include_archived=true" : "")),
  getSite: (siteId) => request(`/sites/${siteId}`),
  createSite: (fields) => request("/sites", { method: "POST", body: fields }),
  // Send only the fields to change; site_publish_date: null clears the date.
  updateSite: (siteId, changes) => request(`/sites/${siteId}`, { method: "PATCH", body: changes }),
  changeSiteStatus: (siteId, status, reason) =>
    request(`/sites/${siteId}/status`, { method: "POST", body: { status, reason } }),
  checkSite: (siteId) => request(`/sites/${siteId}/check`, { method: "POST" }),
  checkAllSites: () => request("/sites/check-all", { method: "POST" }),
  archiveSite: (siteId) => request(`/sites/${siteId}/archive`, { method: "POST" }),
  restoreSite: (siteId) => request(`/sites/${siteId}/restore`, { method: "POST" }),
  siteReport: () => request("/sites/report"),

  listMailingLists: (includeArchived = false) =>
    request("/sites/mailing-lists" + (includeArchived ? "?include_archived=true" : "")),
  createMailingList: (fields) => request("/sites/mailing-lists", { method: "POST", body: fields }),
  // members / site_ids, when given, REPLACE the current sets.
  updateMailingList: (listId, changes) =>
    request(`/sites/mailing-lists/${encodeURIComponent(listId)}`, { method: "PATCH", body: changes }),
  archiveMailingList: (listId) => request(`/sites/mailing-lists/${encodeURIComponent(listId)}/archive`, { method: "POST" }),
  restoreMailingList: (listId) => request(`/sites/mailing-lists/${encodeURIComponent(listId)}/restore`, { method: "POST" }),

  // state: "draft" | "sent" | "dismissed" | "skipped" | "" (all)
  listNotifications: (state = "") => request("/sites/notifications" + (state ? `?state=${state}` : "")),
  sendNotification: (id, message) =>
    request(`/sites/notifications/${id}/send`, { method: "POST", body: message ? { message } : {} }),
  dismissNotification: (id, reason) =>
    request(`/sites/notifications/${id}/dismiss`, { method: "POST", body: reason ? { reason } : {} }),
};
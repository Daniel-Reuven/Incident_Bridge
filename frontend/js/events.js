/**
 * Live-update client: connects to GET /events (Server-Sent Events) and
 * hands each incoming event to a callback. See backend/app/events.py and
 * backend/app/api/events.py for the server side this talks to.
 *
 * client_id identifies this browser TAB, not this user - it's stored in
 * sessionStorage (per-tab, unlike localStorage) so two tabs of the same
 * logged-in user are treated as separate subscribers. It's sent as the
 * X-Client-Id header on every mutating API call (see api.js) so the
 * server can skip echoing a tab's own change back to itself.
 */

const CLIENT_ID_KEY = "ib_client_id";

export function getClientId() {
  let id = sessionStorage.getItem(CLIENT_ID_KEY);
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem(CLIENT_ID_KEY, id);
  }
  return id;
}

/**
 * Opens the SSE connection and calls onEvent(payload) for every live
 * update this tab is meant to see. Returns the EventSource so the caller
 * can close() it if needed (not required on page navigation - the
 * browser tears it down on its own).
 *
 * `scopes` picks which kinds of events reach onEvent. Each event carries a
 * "scope": "sites" for the admin-only site portal (backend/app/api/sites.py),
 * none for incident events, which counts as "incidents". The default,
 * ["incidents"], is what every incident page wants, so the dashboard,
 * incident and pressing pages need no change and never react to site
 * events. The site portal passes { scopes: ["sites", "incidents"] }.
 * (Site events are only ever SENT to admin tabs in the first place - this
 * filter is about which page reacts, not about who may see them.)
 *
 * EventSource reconnects automatically on a dropped connection; nothing
 * extra is needed here for that.
 */
export function connectLiveUpdates(onEvent, { scopes = ["incidents"] } = {}) {
  const source = new EventSource(`/events?client_id=${encodeURIComponent(getClientId())}`);
  source.onmessage = (e) => {
    let payload;
    try {
      payload = JSON.parse(e.data);
    } catch {
      return; // malformed/keepalive payload - ignore
    }
    if (scopes.includes(payload.scope || "incidents")) onEvent(payload);
  };
  return source;
}

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
 * EventSource reconnects automatically on a dropped connection; nothing
 * extra is needed here for that.
 */
export function connectLiveUpdates(onEvent) {
  const source = new EventSource(`/events?client_id=${encodeURIComponent(getClientId())}`);
  source.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      // malformed/keepalive payload - ignore
    }
  };
  return source;
}

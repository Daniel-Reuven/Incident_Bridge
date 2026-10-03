/**
 * The incident page's "Updates" sort order (oldest first / newest first) and
 * where it is remembered.
 *
 * Stored in localStorage - not sessionStorage - on purpose: the choice should
 * survive closing the tab and apply to every incident page, and localStorage
 * is shared by all tabs of the same browser, which is also what lets one tab
 * tell the others when it changes (the "storage" event, see
 * onUpdatesOrderChanged). It is per BROWSER, not per logged-in user: to make
 * it per user, append the username to STORAGE_KEY.
 *
 * Everything here is defensive about storage being unavailable (private
 * mode, blocked cookies): reads fall back to the default order and writes
 * are silently skipped, so the page keeps working - the choice just isn't
 * remembered.
 */

const STORAGE_KEY = "ib_updates_order";
const VALID_ORDERS = new Set(["asc", "desc"]);

/** "asc" = oldest first, the order the server returns comments in (and the original behavior). */
export const DEFAULT_ORDER = "asc";

/** The saved order, or DEFAULT_ORDER if none is saved, the value is invalid, or storage is unavailable. */
export function getUpdatesOrder() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return VALID_ORDERS.has(value) ? value : DEFAULT_ORDER;
  } catch {
    return DEFAULT_ORDER;
  }
}

/** Saves `order` ("asc" or "desc"). Anything else is ignored; a storage failure is ignored too. */
export function setUpdatesOrder(order) {
  if (!VALID_ORDERS.has(order)) return;
  try {
    localStorage.setItem(STORAGE_KEY, order);
  } catch {
    // Storage unavailable: the choice applies to this page view but won't be remembered.
  }
}

/**
 * `comments` arranged for display. The server always sends them oldest
 * first, so "desc" is simply a reversed COPY (the input is never mutated).
 * Reversing - rather than sorting by timestamp - keeps comments that share
 * a timestamp in their true insertion order.
 */
export function sortComments(comments, order) {
  return order === "desc" ? [...comments].reverse() : comments;
}

/**
 * Calls callback(newOrder) whenever ANOTHER tab of this browser changes the
 * saved order (the "storage" event never fires in the tab that made the
 * change), or clears browser storage entirely.
 */
export function onUpdatesOrderChanged(callback) {
  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY || event.key === null) callback(getUpdatesOrder());
  });
}
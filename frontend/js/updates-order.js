/**
 * The incident page's "Updates" sort order (oldest first / newest first).
 *
 * One of the account's interface preferences (backend
 * app/models/preferences.py): saved with the account, so it follows the
 * person to any browser, and kept in js/theme.js's copy while they are
 * logged in. A storage failure only means the choice is not kept in this
 * browser - it is still saved to the account.
 */

import { savePreferences } from "./preferences.js";

const PREFS_KEY = "incidentBridge.prefs";        // js/theme.js's copy

/** "asc" = oldest first, the order the server returns comments in (and the original behavior). */
export const DEFAULT_ORDER = "asc";

/** The account's order (from the copy), or DEFAULT_ORDER. */
export function getUpdatesOrder() {
  return window.ibPrefs ? window.ibPrefs.get().updatesOrder : DEFAULT_ORDER;
}

/** Saves `order` ("asc" or "desc") to the account. Anything else is ignored. */
export function setUpdatesOrder(order) {
  if (order !== "asc" && order !== "desc") return;
  savePreferences({ updatesOrder: order });
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
    if (event.key === PREFS_KEY || event.key === null) callback(getUpdatesOrder());
  });
}
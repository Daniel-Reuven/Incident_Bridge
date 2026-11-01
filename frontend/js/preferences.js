/**
 * The bridge between the account's interface preferences on the server
 * (GET /auth/me, PATCH /auth/preferences) and the browser's copy that
 * js/theme.js applies before each page is drawn.
 *
 *   applyAccountPreferences(prefs)  the server's preferences -> the copy (login, every page load)
 *   savePreferences(changes)        apply at once, then save to the account
 *   forgetSession()                 logout: delete the copy and this tab's remembered filters
 */

import { api } from "./api.js";
import { showToast } from "./toast.js";

const fromServer = (p) => ({ theme: p.theme, darkLevel: p.dark_level, textScale: p.text_scale,
                             updatesOrder: p.updates_order });
const SERVER_NAMES = { theme: "theme", darkLevel: "dark_level", textScale: "text_scale", updatesOrder: "updates_order" };

export function applyAccountPreferences(prefs) {
  if (prefs) window.ibPrefs.load(fromServer(prefs));
}

export async function savePreferences(changes) {
  window.ibPrefs.set(changes);
  const body = Object.fromEntries(Object.entries(changes).map(([name, value]) => [SERVER_NAMES[name], String(value)]));
  try {
    applyAccountPreferences(await api.savePreferences(body));
  } catch (err) {
    showToast(`Your setting applies here, but could not be saved to your account: ${err.message}`);
  }
}

export function forgetSession() {
  window.ibPrefs.clear();
  try {
    localStorage.removeItem("ib_updates_order");     // the per-browser copy of versions before 0.6.0
  } catch {
    // storage unavailable
  }
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith("dashboard.") || key === "logs.filters") sessionStorage.removeItem(key);
    }
  } catch {
    // sessionStorage unavailable: nothing was kept
  }
}

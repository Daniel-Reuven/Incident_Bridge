/*
 * Interface preferences: color scheme (light / dark / system), text scale
 * and the incident page's updates order.
 *
 * They belong to the ACCOUNT (stored on the server - backend
 * app/models/preferences.py, PATCH /auth/preferences). This browser keeps a
 * COPY of the logged-in user's preferences in localStorage, only so the
 * theme can be applied before the page is drawn - no flash of the light
 * theme on a dark-mode page. js/preferences.js fills the copy from the
 * server at login and on every page load, and deletes it at logout.
 *
 * The login page (<html data-login-page>) ignores the copy and always uses
 * the system's color scheme at the normal size: whoever logs in next starts
 * from a clean screen, even if the last session ended without a logout.
 *
 * Loaded as a plain (non-module) script in the <head> of EVERY page. It
 * must stay tiny and must not wait for anything.
 *
 * "system" follows the operating system's setting, live (matchMedia).
 * The text scale uses CSS zoom on <html>, which scales every pixel size the
 * stylesheet uses.
 *
 * window.ibPrefs: { get(), set(changes), load(prefs), clear(), THEMES, TEXT_SCALES }
 *   prefs: { theme, textScale, updatesOrder }
 */
(function () {
  var THEMES = ["system", "light", "dark"];
  var TEXT_SCALES = ["0.9", "1", "1.15", "1.3"];
  var ORDERS = ["asc", "desc"];
  var KEY = "incidentBridge.prefs";
  var loginPage = document.documentElement.hasAttribute("data-login-page");
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function defaults() {
    return { theme: "system", textScale: "1", updatesOrder: "asc" };
  }

  function clean(stored) {
    var prefs = defaults();
    if (stored && THEMES.indexOf(stored.theme) !== -1) prefs.theme = stored.theme;
    if (stored && TEXT_SCALES.indexOf(String(stored.textScale)) !== -1) prefs.textScale = String(stored.textScale);
    if (stored && ORDERS.indexOf(stored.updatesOrder) !== -1) prefs.updatesOrder = stored.updatesOrder;
    return prefs;
  }

  function get() {
    if (loginPage) return defaults();
    try {
      return clean(JSON.parse(localStorage.getItem(KEY) || "{}"));
    } catch (e) {
      return defaults();       // storage blocked or corrupt
    }
  }

  function apply() {
    var prefs = get();
    var dark = prefs.theme === "dark" || (prefs.theme === "system" && darkQuery && darkQuery.matches);
    var root = document.documentElement;
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.style.zoom = prefs.textScale === "1" ? "" : prefs.textScale;
  }

  function store(prefs) {
    try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) { /* applies to this page only */ }
    apply();
    return prefs;
  }

  /** Change some preferences in the copy (js/preferences.js then saves them to the account). */
  function set(changes) {
    var prefs = get();
    for (var name in changes) if (Object.prototype.hasOwnProperty.call(changes, name)) prefs[name] = String(changes[name]);
    return store(clean(prefs));
  }

  /** Replace the copy with the account's preferences (after login, and on every page load). */
  function load(prefs) {
    return store(clean(prefs));
  }

  /** Forget the copy (logout): back to the system scheme and the normal size. */
  function clear() {
    try { localStorage.removeItem(KEY); } catch (e) { /* nothing stored */ }
    apply();
  }

  if (darkQuery) {
    var onChange = function () { if (get().theme === "system") apply(); };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onChange);
    else if (darkQuery.addListener) darkQuery.addListener(onChange);
  }
  // Another tab changed the settings, or logged out: follow it.
  window.addEventListener("storage", function (e) { if (e.key === KEY || e.key === null) apply(); });

  window.ibPrefs = { get: get, set: set, load: load, clear: clear, THEMES: THEMES, TEXT_SCALES: TEXT_SCALES };
  apply();
})();

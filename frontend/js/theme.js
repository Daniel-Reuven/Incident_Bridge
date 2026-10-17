/*
 * Interface preferences: color scheme (light / dark / system) and text
 * scale - set in Settings > Interface (js/settings.js) and kept in this
 * browser's localStorage, so they apply to every page and every account
 * used in this browser.
 *
 * Loaded as a plain (non-module) script in the <head> of EVERY page, so the
 * theme is applied before the page is drawn - no flash of the light theme
 * on a dark-mode page. It must stay tiny and must not wait for anything.
 *
 * "system" follows the operating system's setting, live (matchMedia).
 * The text scale uses CSS zoom on <html>, which scales every pixel size the
 * stylesheet uses.
 *
 * window.ibPrefs: { get(), set({theme, textScale}), THEMES, TEXT_SCALES }
 */
(function () {
  var THEMES = ["system", "light", "dark"];
  var TEXT_SCALES = ["0.9", "1", "1.15", "1.3"];
  var KEY = "incidentBridge.prefs";
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function get() {
    var prefs = { theme: "system", textScale: "1" };
    try {
      var stored = JSON.parse(localStorage.getItem(KEY) || "{}");
      if (THEMES.indexOf(stored.theme) !== -1) prefs.theme = stored.theme;
      if (TEXT_SCALES.indexOf(String(stored.textScale)) !== -1) prefs.textScale = String(stored.textScale);
    } catch (e) { /* storage blocked or corrupt: defaults */ }
    return prefs;
  }

  function apply() {
    var prefs = get();
    var dark = prefs.theme === "dark" || (prefs.theme === "system" && darkQuery && darkQuery.matches);
    var root = document.documentElement;
    root.setAttribute("data-theme", dark ? "dark" : "light");
    root.style.zoom = prefs.textScale === "1" ? "" : prefs.textScale;
  }

  function set(changes) {
    var prefs = get();
    if (changes.theme !== undefined) prefs.theme = changes.theme;
    if (changes.textScale !== undefined) prefs.textScale = String(changes.textScale);
    try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) { /* applies to this page only */ }
    apply();
    return prefs;
  }

  if (darkQuery) {
    var onChange = function () { if (get().theme === "system") apply(); };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onChange);
    else if (darkQuery.addListener) darkQuery.addListener(onChange);
  }
  // Another tab changed the settings: follow it.
  window.addEventListener("storage", function (e) { if (e.key === KEY) apply(); });

  window.ibPrefs = { get: get, set: set, THEMES: THEMES, TEXT_SCALES: TEXT_SCALES };
  apply();
})();

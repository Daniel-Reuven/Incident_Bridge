/**
 * The site footer, shown at the bottom of EVERY page (logged in or not):
 *
 *   Incident Bridge v0.4.8 · Guide · About · Terms · MIT License · GitHub
 *
 * It mounts itself: a page only needs
 *     <script type="module" src="/js/footer.js"></script>
 * and the footer is appended to the end of <body>. The links come from
 * SITE.pages in site-info.js, and the version from the server (GET /health),
 * so nothing here needs editing when a page is added or the version bumped.
 * The current page's link is marked with aria-current="page".
 *
 * New version -> fresh page: the server sends every interface file with
 * "Cache-Control: no-cache" (backend/app/api/security.py), so the browser
 * re-checks each file with the server and never keeps using an old copy.
 * A tab that was already open when the server was upgraded would still run
 * the old code, so watchForNewVersion() asks GET /health again whenever the
 * tab comes back into view and every minute, and reloads the page once the
 * version differs from the one the page was loaded with.
 */
import { SITE, appVersion, licenseUrl } from "./site-info.js";

export function mountFooter() {
  if (document.querySelector(".site-footer")) return;          // already there
  const here = window.location.pathname;
  const pageLinks = SITE.pages
    .map((p) => `<a href="${p.href}"${p.href === here ? ' aria-current="page"' : ""}>${p.label}</a>`)
    .join("");

  const footer = document.createElement("footer");
  footer.className = "site-footer";
  footer.innerHTML = `
    <span class="site-footer-name">${SITE.name}<span class="site-footer-version"></span></span>
    <nav class="site-footer-links" aria-label="About this app">
      ${pageLinks}
      <a href="${licenseUrl()}" target="_blank" rel="noopener">${SITE.license.name}</a>
      <a href="${SITE.repository}" target="_blank" rel="noopener">GitHub</a>
    </nav>`;
  document.body.appendChild(footer);

  appVersion().then((version) => {
    if (version) {
      footer.querySelector(".site-footer-version").textContent = ` v${version}`;
      watchForNewVersion(version);
    }
  });
}

const VERSION_CHECK_MS = 60_000;

function watchForNewVersion(loadedVersion) {
  let reloading = false;
  const check = async () => {
    if (reloading || document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/health", { cache: "no-store", credentials: "same-origin" });
      const { version } = await response.json();
      if (version && version !== loadedVersion) {
        reloading = true;
        window.location.reload();
      }
    } catch {
      // server unreachable (restarting?) - try again next time
    }
  };
  setInterval(check, VERSION_CHECK_MS);
  document.addEventListener("visibilitychange", check);
}

mountFooter();

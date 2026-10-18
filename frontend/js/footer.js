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
    if (version) footer.querySelector(".site-footer-version").textContent = ` v${version}`;
  });
}

mountFooter();

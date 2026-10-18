/**
 * Shared script for the PUBLIC pages - guide.html, about.html, terms.html.
 * They open without logging in (unlike the app pages, whose topbar.js
 * sends a logged-out visitor to the login page).
 *
 * What it does when loaded:
 * 1. Renders the header into <header id="public-header">: the wordmark, the
 *    links to the public pages (the current one marked), and on the right
 *    either "Log in" (logged out) or the username and "Back to dashboard"
 *    (logged in - checked with GET /auth/me, a 401 just means logged out).
 * 2. Fills placeholders from site-info.js, so the pages never repeat those facts:
 *      <span data-site="version"></span>     the app version, from GET /health
 *      <span data-site="name"></span>        "Incident Bridge"
 *      <span data-site="course"></span>      the course line
 *      <ul data-site="team"></ul>            the team, linked to GitHub profiles
 *      <a data-site="repository"></a>        the repository link (text = URL if empty)
 *      <a data-site="license"></a>           the license file on GitHub
 *
 * Usage, at the end of a public page's <body> (footer.js adds the footer):
 *   <script type="module" src="/js/public-page.js"></script>
 *   <script type="module" src="/js/footer.js"></script>
 */
import { SITE, appVersion, githubProfile, licenseUrl } from "./site-info.js";

async function currentUser() {
  try {
    const res = await fetch("/auth/me", { credentials: "same-origin" });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export async function mountPublicHeader() {
  const el = document.getElementById("public-header");
  if (!el) return;
  const here = window.location.pathname;
  const links = SITE.pages
    .map((p) => `<a href="${p.href}"${p.href === here ? ' aria-current="page"' : ""}>${p.label}</a>`)
    .join("");
  el.innerHTML = `
    <div class="topbar">
      <div class="public-nav">
        <a class="wordmark" href="/">${SITE.name}</a>
        <nav class="public-links" aria-label="Pages">${links}</nav>
      </div>
      <div class="topbar-user" id="public-account"></div>
    </div>`;

  const user = await currentUser();
  const account = el.querySelector("#public-account");
  if (user) {
    const name = document.createElement("span");
    name.className = "topbar-username";
    name.textContent = user.username;                       // text, never HTML
    account.append(name);
    account.insertAdjacentHTML("beforeend", `<a class="btn btn-ghost btn-sm" href="/dashboard.html">Back to dashboard</a>`);
  } else {
    account.innerHTML = `<a class="btn btn-primary btn-sm" href="/">Log in</a>`;
  }
}

export function fillSiteInfo(root = document) {
  root.querySelectorAll('[data-site="name"]').forEach((e) => { e.textContent = SITE.name; });
  root.querySelectorAll('[data-site="course"]').forEach((e) => { e.textContent = SITE.course; });
  root.querySelectorAll('[data-site="repository"]').forEach((e) => {
    e.href = SITE.repository;
    if (!e.textContent.trim()) e.textContent = SITE.repository.replace("https://", "");
  });
  root.querySelectorAll('[data-site="license"]').forEach((e) => {
    e.href = licenseUrl();
    if (!e.textContent.trim()) e.textContent = SITE.license.name;
  });
  root.querySelectorAll('[data-site="team"]').forEach((list) => {
    list.innerHTML = SITE.team
      .map((m) => `<li><a href="${githubProfile(m.github)}" target="_blank" rel="noopener">@${m.github}</a></li>`)
      .join("");
  });
  const versions = root.querySelectorAll('[data-site="version"]');
  if (versions.length) {
    appVersion().then((v) => versions.forEach((e) => { e.textContent = v ? `v${v}` : "unknown"; }));
  }
}

mountPublicHeader();
fillSiteInfo();

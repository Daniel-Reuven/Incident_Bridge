/**
 * Facts about the project itself, in ONE place - the footer (footer.js),
 * the About page (about.html) and the Terms page (terms.html) all read
 * them from here, so a change (a new team member, a new repository URL)
 * is a one-line edit.
 *
 * The version is NOT written here: appVersion() asks the server
 * (GET /health), which reads it from backend/pyproject.toml - the single
 * place it is bumped (see backend/app/version.py).
 *
 * Usage:
 *   import { SITE, appVersion } from "./site-info.js";
 *   SITE.team.forEach((m) => ...);
 *   const version = await appVersion();   // "0.4.8", or null if the server can't be reached
 */

export const SITE = {
  name: "Incident Bridge",
  repository: "https://github.com/Daniel-Reuven/Incident_Bridge",
  license: { name: "MIT License", file: "LICENSE" },
  course: "Advanced Programming — course project",
  // The team, as GitHub usernames (each links to the profile). Add a member with one more line.
  team: [
    { github: "Daniel-Reuven" },
    { github: "OfirWasHere" },
    { github: "roi2109" },
  ],
  // The public pages, in the order the header and footer list them.
  pages: [
    { href: "/guide.html", label: "Guide" },
    { href: "/about.html", label: "About" },
    { href: "/terms.html", label: "Terms" },
  ],
};

/** The license file on GitHub (the branch the repository shows by default). */
export const licenseUrl = () => `${SITE.repository}/blob/HEAD/${SITE.license.file}`;

/** A GitHub profile URL. */
export const githubProfile = (username) => `https://github.com/${encodeURIComponent(username)}`;

let versionPromise = null;

/** The app's version from GET /health, fetched once per page; null if it can't be read. */
export function appVersion() {
  if (!versionPromise) {
    versionPromise = fetch("/health", { credentials: "same-origin" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => (data && data.version) || null)
      .catch(() => null);
  }
  return versionPromise;
}

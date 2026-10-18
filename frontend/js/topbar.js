import { api } from "./api.js";
import { openSettings } from "./settings.js";

/**
 * Renders the top bar into #topbar and enforces the auth guard: if the
 * session isn't valid, redirects to the login page and returns null
 * instead of throwing, so callers can just check the return value.
 *
 * Buttons: the username; for admins "Site portal", "Users" and "Logs" (styled with
 * the admin-action color, so they read as admin-only areas - not rendered
 * for other roles, and their APIs refuse them with 403 anyway); "Settings"
 * (password and interface - js/settings.js); and "Log out" on its own, as
 * a frequent, low-risk action that shouldn't require opening a dialog.
 * The admin button for the current page is marked as such.
 *
 * Forced password change: when the server says this user must change their
 * password (first login, or after an admin reset), the Settings dialog
 * opens on its Password tab and cannot be closed, and this returns null so
 * the page loads no data (the API would refuse it with 403 anyway). After
 * the change the page reloads normally.
 */
export async function mountTopbar() {
  let user;
  try {
    user = await api.me();
  } catch {
    window.location.href = "/";
    return null;
  }

  const here = window.location.pathname;
  const adminButton = (href, label) =>
    `<a class="btn btn-admin btn-sm" href="${href}"${here === href ? ' aria-current="page"' : ""}>${label}</a>`;
  const adminButtons = user.role === "admin"
    ? adminButton("/sites.html", "Site portal") + adminButton("/users.html", "Users") + adminButton("/logs.html", "Logs")
    : "";

  const el = document.getElementById("topbar");
  el.innerHTML = `
    <div class="topbar">
      <a class="wordmark" href="/dashboard.html">Incident Bridge</a>
      <div class="topbar-user">
        <span class="topbar-username">${escapeHtml(user.username)}</span>
        ${adminButtons}
        <span class="topbar-role">${user.role}</span>
        <button class="btn btn-ghost btn-sm" data-action="settings">Settings</button>
        <button class="btn btn-ghost btn-sm" data-action="logout">Log out</button>
      </div>
    </div>
  `;

  el.querySelector('[data-action="settings"]').addEventListener("click", () => openSettings(user));
  el.querySelector('[data-action="logout"]').addEventListener("click", async () => {
    await api.logout();
    window.location.href = "/";
  });

  if (user.must_change_password) {
    await openSettings(user, { forced: true });
    return null;
  }
  return user;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

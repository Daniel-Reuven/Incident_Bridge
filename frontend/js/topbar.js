import { api } from "./api.js";

/**
 * Renders the top bar into #topbar and enforces the auth guard: if the
 * session isn't valid, redirects to the login page and returns null
 * instead of throwing, so callers can just check the return value.
 *
 * Log out lives as its own top-bar button, separate from the Account
 * menu/dialog - it's a frequent, low-risk action that shouldn't require
 * opening a dialog first.
 *
 * Admins also get a "Site portal" button right next to their username,
 * styled with the admin-action color so it reads as an admin-only area.
 * It is simply not rendered for other roles (the portal's API refuses
 * them with 403 anyway, and sites.js sends them back to the dashboard).
 * On the portal page itself it is marked as the current page.
 */
export async function mountTopbar() {
  let user;
  try {
    user = await api.me();
  } catch {
    window.location.href = "/";
    return null;
  }

  const onPortal = window.location.pathname === "/sites.html";
  const portalButton = user.role === "admin"
    ? `<a class="btn btn-admin btn-sm" href="/sites.html"${onPortal ? ' aria-current="page"' : ""}>Site portal</a>`
    : "";

  const el = document.getElementById("topbar");
  el.innerHTML = `
    <div class="topbar">
      <a class="wordmark" href="/dashboard.html">Incident Bridge</a>
      <div class="topbar-user">
        <span class="topbar-username">${user.username}</span>
        ${portalButton}
        <span class="topbar-role">${user.role}</span>
        <button class="btn btn-ghost btn-sm" data-action="account">Account</button>
        <button class="btn btn-ghost btn-sm" data-action="logout">Log out</button>
      </div>
    </div>
    <dialog id="account-dialog" class="dialog">
      <form class="dialog-form" id="account-form">
        <h2>Change password</h2>
        <label>Current password<input type="password" name="current_password" required></label>
        <label>New password<input type="password" name="new_password" required minlength="8"></label>
        <p class="hint">At least 8 characters, letters and numbers only.</p>
        <p class="form-error" id="account-error" hidden></p>
        <div class="dialog-actions">
          <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button type="submit" class="btn btn-primary">Save changes</button>
        </div>
      </form>
    </dialog>
  `;

  const dialog = el.querySelector("#account-dialog");
  const form = el.querySelector("#account-form");
  const errorEl = el.querySelector("#account-error");

  el.querySelector('[data-action="account"]').addEventListener("click", () => {
    form.reset();
    errorEl.hidden = true;
    dialog.showModal();
  });
  el.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());
  el.querySelector('[data-action="logout"]').addEventListener("click", async () => {
    await api.logout();
    window.location.href = "/";
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    try {
      await api.changePassword(form.current_password.value, form.new_password.value);
      dialog.close();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });

  return user;
}

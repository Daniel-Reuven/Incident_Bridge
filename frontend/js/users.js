/**
 * Users page (admin only): every account, and creating, promoting/demoting,
 * disabling/enabling and resetting passwords - through /users
 * (backend/app/api/users.py), which enforces every rule again.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { passwordFieldsHtml, wirePasswordFields } from "./password-fields.js";
import { formatWhen } from "./datetime.js";
import { showToast } from "./toast.js";

// Declared before the top-level await below runs the page code that uses them.
let resetTarget = null;    // the username the Reset password dialog is for
let resetFields = null;

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  document.getElementById("users-page").hidden = false;
  await wireNewUserDialog();
  await wireResetDialog();
  wireTableActions();
  await load();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function load() {
  const accounts = await api.listUsers();
  document.getElementById("users-body").innerHTML = accounts.map((account) => {
    const self = account.username === user.username;
    const status = [account.disabled ? "Disabled" : "Active",
                    account.must_change_password ? "must change password" : ""].filter(Boolean).join(" · ");
    const actions = self
      ? `<span class="hint">This is you - use Settings</span>`
      : `<button class="btn btn-ghost btn-sm" data-action="role">${account.role === "admin" ? "Make user" : "Make admin"}</button>
         <button class="btn btn-ghost btn-sm" data-action="disable">${account.disabled ? "Enable" : "Disable"}</button>
         <button class="btn btn-ghost btn-sm" data-action="reset">Reset password</button>`;
    return `<tr data-username="${escapeHtml(account.username)}" data-role="${account.role}"
                data-disabled="${account.disabled ? "1" : ""}" class="${account.disabled ? "user-disabled" : ""}">
      <td>${escapeHtml(account.username)}${self ? ' <span class="hint">(you)</span>' : ""}</td>
      <td>${account.role === "admin" ? "Admin" : "User"}</td>
      <td>${status}</td>
      <td>${formatWhen(account.created_at)}</td>
      <td class="user-actions"><div class="user-actions-inner">${actions}</div></td>
    </tr>`;
  }).join("");
}

function wireTableActions() {
  document.getElementById("users-body").addEventListener("click", async (e) => {
    const button = e.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest("tr");
    const username = row.dataset.username;
    try {
      if (button.dataset.action === "role") {
        const role = row.dataset.role === "admin" ? "user" : "admin";
        if (!confirm(`Make ${username} ${role === "admin" ? "an admin" : "a regular user"}?`)) return;
        await api.updateUser(username, { role });
        showToast(`${username} is now ${role === "admin" ? "an admin" : "a regular user"}.`);
      } else if (button.dataset.action === "disable") {
        const disable = !row.dataset.disabled;
        if (disable && !confirm(`Disable ${username}? They are signed out now and cannot log in until enabled.`)) return;
        await api.updateUser(username, { disabled: disable });
        showToast(`${username} is ${disable ? "disabled" : "enabled"}.`);
      } else if (button.dataset.action === "reset") {
        openReset(username);
        return;
      }
      await load();
    } catch (err) {
      alert(err.message);
    }
  });
}

async function wireNewUserDialog() {
  const dialog = document.getElementById("new-user-dialog");
  const form = document.getElementById("new-user-form");
  const errorEl = document.getElementById("new-user-error");
  form.querySelector(".pw-fields").innerHTML = passwordFieldsHtml({ withCurrent: false, newLabel: "Password" });
  const fields = await wirePasswordFields(form, { username: () => form.username.value });
  form.username.addEventListener("input", () => fields.ready());

  document.getElementById("new-user-btn").addEventListener("click", () => {
    form.reset();
    fields.reset();
    errorEl.hidden = true;
    dialog.showModal();
  });
  form.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    try {
      const created = await api.createUser(form.username.value.trim(), form.role.value,
                                           form.new_password.value, form.confirm_password.value);
      dialog.close();
      showToast(created.must_change_password ? `${created.username} created - they must change the password at their first login.`
                                             : `${created.username} created.`);
      await load();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });
}


async function wireResetDialog() {
  const dialog = document.getElementById("reset-dialog");
  const form = document.getElementById("reset-form");
  const errorEl = document.getElementById("reset-error");
  form.querySelector(".pw-fields").innerHTML = passwordFieldsHtml({ withCurrent: false });
  resetFields = await wirePasswordFields(form, { username: () => resetTarget || "" });
  form.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    try {
      const account = await api.resetUserPassword(resetTarget, form.new_password.value, form.confirm_password.value);
      dialog.close();
      showToast(account && account.must_change_password === false ? `Password of ${resetTarget} reset.`
                : `Password of ${resetTarget} reset - they must change it at their next login.`);
      await load();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });
}

function openReset(username) {
  resetTarget = username;
  document.getElementById("reset-title").textContent = `Reset password of ${username}`;
  document.getElementById("reset-error").hidden = true;
  resetFields.reset();
  document.getElementById("reset-dialog").showModal();
}

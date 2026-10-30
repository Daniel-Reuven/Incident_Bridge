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
import { loadPeople, partsHtml } from "./people.js";

const DETAIL_NAMES = ["first_name", "middle_name", "last_name", "job_title", "location"];
const SAMPLE = { username: "dlevi", first_name: "Dana", middle_name: "", last_name: "Levi",
                 job_title: "Technician", location: "Haifa office" };

// Declared before the top-level await below runs the page code that uses them.
let resetTarget = null;    // the username the Reset password dialog is for
let resetFields = null;
let formatInfo = null;     // GET /users/display-format: the default format, presets, placeholders
let accountsByName = {};
let detailsTarget = null;  // the username the Details dialog is for
let defaultEditor = null;
let ownEditor = null;

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  document.getElementById("users-page").hidden = false;
  formatInfo = await api.displayFormat();
  wireDefaultFormat();
  wireDetailsDialog();
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
  accountsByName = Object.fromEntries(accounts.map((a) => [a.username, a]));
  document.getElementById("users-body").innerHTML = accounts.map((account) => {
    const self = account.username === user.username;
    const status = [account.disabled ? "Disabled" : "Active",
                    account.must_change_password ? "must change password" : ""].filter(Boolean).join(" · ");
    const details = `<button class="btn btn-ghost btn-sm" data-action="details">Details</button>`;
    const actions = self
      ? `${details}<span class="hint">Password: use Settings</span>`
      : `${details}<button class="btn btn-ghost btn-sm" data-action="role">${account.role === "admin" ? "Make user" : "Make admin"}</button>
         <button class="btn btn-ghost btn-sm" data-action="disable">${account.disabled ? "Enable" : "Disable"}</button>
         <button class="btn btn-ghost btn-sm" data-action="reset">Reset password</button>`;
    return `<tr data-username="${escapeHtml(account.username)}" data-role="${account.role}"
                data-disabled="${account.disabled ? "1" : ""}" class="${account.disabled ? "user-disabled" : ""}">
      <td><span class="person">${partsHtml(account.display_parts)}</span>${self ? ' <span class="hint">(you)</span>' : ""}
        ${account.display_name !== account.username ? `<div class="hint user-login">${escapeHtml(account.username)}</div>` : ""}</td>
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
      } else if (button.dataset.action === "details") {
        openDetails(username);
        return;
      }
      await loadPeople();
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
      const details = Object.fromEntries(DETAIL_NAMES.map((name) => [name, form[name].value.trim()]));
      const created = await api.createUser(form.username.value.trim(), form.role.value,
                                           form.new_password.value, form.confirm_password.value, details);
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


// --- display names (backend/app/display_names.py) -----------------------------------------------

/**
 * A format editor in `container`: the presets, "Custom", the text itself and
 * buttons that insert a placeholder where the cursor is. onChange(format)
 * runs on every change. Returns { value(), set(format) }.
 */
function formatEditor(container, onChange) {
  container.innerHTML = `
    <label>Format
      <select class="format-preset">
        ${formatInfo.presets.map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("")}
        <option value="">Custom…</option>
      </select></label>
    <label class="format-custom">Custom format
      <input type="text" class="format-text" maxlength="120" autocomplete="off" spellcheck="false"></label>
    <div class="format-chips" aria-label="Insert a field">
      ${formatInfo.placeholders.map((p) => `<button type="button" class="chip" data-insert="{${p}}">{${p}}</button>`).join("")}
    </div>`;
  const select = container.querySelector(".format-preset");
  const text = container.querySelector(".format-text");
  const sync = () => {
    select.value = formatInfo.presets.includes(text.value) ? text.value : "";
    onChange(text.value);
  };
  select.addEventListener("change", () => {
    if (select.value) text.value = select.value;
    else text.focus();
    sync();
  });
  text.addEventListener("input", sync);
  container.querySelectorAll("[data-insert]").forEach((chip) => chip.addEventListener("click", () => {
    const at = text.selectionStart ?? text.value.length;
    text.value = text.value.slice(0, at) + chip.dataset.insert + text.value.slice(text.selectionEnd ?? at);
    text.focus();
    text.selectionStart = text.selectionEnd = at + chip.dataset.insert.length;
    sync();
  }));
  return {
    value: () => text.value.trim(),
    set: (format) => { text.value = format; select.value = formatInfo.presets.includes(format) ? format : ""; },
  };
}

function debounce(fn, ms = 250) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

async function preview(target, body) {
  try {
    const result = await api.previewDisplay(body);
    target.innerHTML = partsHtml(result.display_parts);
  } catch (err) {
    target.textContent = err.message;
  }
}

function wireDefaultFormat() {
  const container = document.getElementById("default-format-editor");
  const shown = document.createElement("p");
  shown.className = "format-preview";
  shown.innerHTML = `For example: <strong id="default-preview"></strong>`;
  const update = debounce((format) => preview(document.getElementById("default-preview"),
                                               { ...SAMPLE, format: format || null }));
  defaultEditor = formatEditor(container, update);
  container.append(shown);
  defaultEditor.set(formatInfo.format);
  update(formatInfo.format);
  document.getElementById("save-format-btn").addEventListener("click", async () => {
    try {
      formatInfo.format = (await api.setDisplayFormat(defaultEditor.value())).format;
      showToast("Display name format saved - everyone without a format of their own is shown this way.");
      await loadPeople();
      await load();
    } catch (err) {
      alert(err.message);
    }
  });
}

function wireDetailsDialog() {
  const dialog = document.getElementById("details-dialog");
  const form = document.getElementById("details-form");
  const errorEl = document.getElementById("details-error");
  const own = document.getElementById("own-format-editor");
  const ownMode = () => form.querySelector('input[name="format_mode"]:checked').value === "own";
  const update = debounce(() => {
    const body = Object.fromEntries(DETAIL_NAMES.map((name) => [name, form[name].value]));
    preview(document.getElementById("details-preview"),
            { ...body, username: detailsTarget || "", format: ownMode() ? (ownEditor.value() || null) : null });
  });
  ownEditor = formatEditor(own, update);
  form.addEventListener("input", update);
  form.querySelectorAll('input[name="format_mode"]').forEach((radio) => radio.addEventListener("change", () => {
    own.hidden = !ownMode();
    if (ownMode() && !ownEditor.value()) ownEditor.set(formatInfo.format);
    update();
  }));
  form.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    const details = Object.fromEntries(DETAIL_NAMES.map((name) => [name, form[name].value.trim()]));
    details.display_format = ownMode() ? ownEditor.value() : "";
    try {
      await api.updateUserDetails(detailsTarget, details);
      dialog.close();
      showToast(`Details of ${detailsTarget} saved.`);
      await loadPeople();
      await load();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });
  dialog.update = update;
}

function openDetails(username) {
  const account = accountsByName[username];
  const dialog = document.getElementById("details-dialog");
  const form = document.getElementById("details-form");
  detailsTarget = username;
  document.getElementById("details-title").textContent = `Details of ${username}`;
  document.getElementById("details-error").hidden = true;
  DETAIL_NAMES.forEach((name) => { form[name].value = account[name] || ""; });
  const hasOwn = Boolean(account.display_format);
  form.querySelector(`input[name="format_mode"][value="${hasOwn ? "own" : "default"}"]`).checked = true;
  document.getElementById("own-format-editor").hidden = !hasOwn;
  ownEditor.set(account.display_format || formatInfo.format);
  dialog.update();
  dialog.showModal();
}

/**
 * Settings dialog (the top bar's "Settings" button), two tabs:
 *
 *   Password   current password, new password and its confirmation, with
 *              the policy tooltip and live checklist (password-fields.js).
 *              Saving ends this user's other sessions (the server bumps
 *              their session version) - this one stays logged in.
 *   Interface  color scheme (System / Light / Dark) and text size, applied
 *              at once and saved with the account (js/preferences.js).
 *
 * Forced mode (openSettings({ forced: true })): when the server says the
 * user must change their password (first login, or after an admin reset),
 * the dialog opens on the Password tab and cannot be closed until the
 * password is changed; the page then reloads.
 */
import { api } from "./api.js";
import { passwordFieldsHtml, wirePasswordFields } from "./password-fields.js";
import { showToast } from "./toast.js";
import { savePreferences } from "./preferences.js";
import { personText } from "./people.js";

const THEME_LABELS = { system: "System", light: "Light", dark: "Dark" };
const SCALE_LABELS = { "0.9": "Small (90%)", "1": "Normal (100%)", "1.15": "Large (115%)", "1.3": "Larger (130%)" };

let dialog = null;
let fields = null;

export async function openSettings(user, { forced = false, tab = "interface" } = {}) {
  if (!dialog) await build(user);
  dialog.classList.toggle("settings-forced", forced);
  dialog.querySelector(".settings-forced-note").hidden = !forced;
  dialog.querySelector('[data-action="close"]').hidden = forced;
  dialog.querySelector('#settings-password-form [data-action="cancel"]').hidden = forced;
  dialog.dataset.forced = forced ? "1" : "";
  fields.reset();
  dialog.querySelector("#settings-password-error").hidden = true;
  syncInterface();
  showTab(forced ? "password" : tab);
  dialog.showModal();
}

async function build(user) {
  dialog = document.createElement("dialog");
  dialog.className = "dialog dialog-wide settings-dialog";
  dialog.setAttribute("aria-labelledby", "settings-title");
  dialog.innerHTML = `
    <div class="dialog-form">
      <div class="settings-head">
        <h2 id="settings-title">Settings</h2>
        <button type="button" class="btn btn-ghost btn-sm" data-action="close">Close</button>
      </div>
      <p class="settings-forced-note" hidden>Welcome, ${escapeHtml(personText(user.username))}. Before you continue, replace the
        password you were given with one only you know.</p>
      <nav class="tabs" role="tablist" aria-label="Settings sections">
        <button class="tab" role="tab" id="settings-tab-interface" aria-controls="settings-panel-interface">Interface</button>
        <button class="tab" role="tab" id="settings-tab-password" aria-controls="settings-panel-password">Password</button>
      </nav>

      <section role="tabpanel" id="settings-panel-password" aria-labelledby="settings-tab-password">
        <form id="settings-password-form">
          ${passwordFieldsHtml({ withCurrent: true })}
          <p class="hint">Saving signs you out everywhere else.</p>
          <p class="form-error" id="settings-password-error" hidden></p>
          <div class="dialog-actions">
            <button type="button" class="btn btn-ghost" data-action="cancel">Cancel</button>
            <button type="submit" class="btn btn-primary">Change password</button>
          </div>
        </form>
      </section>

      <section role="tabpanel" id="settings-panel-interface" aria-labelledby="settings-tab-interface" hidden>
        <fieldset class="choice-group">
          <legend>Color scheme</legend>
          ${window.ibPrefs.THEMES.map((value) => `
            <label class="choice"><input type="radio" name="theme" value="${value}"> ${THEME_LABELS[value]}</label>`).join("")}
          <p class="hint">System follows your computer's light or dark setting.</p>
        </fieldset>
        <fieldset class="choice-group">
          <legend>Text size</legend>
          ${window.ibPrefs.TEXT_SCALES.map((value) => `
            <label class="choice"><input type="radio" name="text-scale" value="${value}"> ${SCALE_LABELS[value]}</label>`).join("")}
        </fieldset>
        <p class="hint">Saved with your account, for every page and every computer you log in from. Changes apply right away.</p>
      </section>
    </div>`;
  document.body.appendChild(dialog);

  const form = dialog.querySelector("#settings-password-form");
  fields = await wirePasswordFields(form, { username: () => user.username });

  dialog.querySelectorAll('[role="tab"]').forEach((tab) =>
    tab.addEventListener("click", () => showTab(tab.id.replace("settings-tab-", ""))));
  dialog.querySelector('[data-action="close"]').addEventListener("click", () => dialog.close());
  form.querySelector('[data-action="cancel"]').addEventListener("click", () => dialog.close());
  // A forced change cannot be dismissed with Escape.
  dialog.addEventListener("cancel", (e) => { if (dialog.dataset.forced) e.preventDefault(); });

  dialog.querySelectorAll('input[name="theme"]').forEach((input) =>
    input.addEventListener("change", () => savePreferences({ theme: input.value })));
  dialog.querySelectorAll('input[name="text-scale"]').forEach((input) =>
    input.addEventListener("change", () => savePreferences({ textScale: input.value })));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = dialog.querySelector("#settings-password-error");
    errorEl.hidden = true;
    try {
      await api.changePassword(form.current_password.value, form.new_password.value, form.confirm_password.value);
      const wasForced = Boolean(dialog.dataset.forced);
      dialog.close();
      if (wasForced) window.location.reload();
      else showToast("Password changed. You are signed out everywhere else.");
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  });
}

function showTab(name) {
  dialog.querySelectorAll('[role="tab"]').forEach((tab) => {
    const selected = tab.id === `settings-tab-${name}`;
    tab.setAttribute("aria-selected", String(selected));
    dialog.querySelector(`#${tab.getAttribute("aria-controls")}`).hidden = !selected;
  });
}

function syncInterface() {
  const prefs = window.ibPrefs.get();
  dialog.querySelectorAll('input[name="theme"]').forEach((input) => { input.checked = input.value === prefs.theme; });
  dialog.querySelectorAll('input[name="text-scale"]').forEach((input) => { input.checked = input.value === prefs.textScale; });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

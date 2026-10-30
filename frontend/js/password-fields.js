/**
 * The password fields shared by every form that sets a password: Settings >
 * Password (with the current password), and the admin's New user / Reset
 * password dialogs (users.js, without it).
 *
 *   - "New password" has an info button (ⓘ) whose tooltip lists the policy
 *     and links to its full description in the guide (/guide.html#password-policy);
 *   - a live checklist ticks off each rule while typing;
 *   - "Confirm new password" says whether the two match;
 *   - "Show passwords" switches every field of the form to plain text.
 *
 * The rules come from the server (GET /auth/password-policy, the same
 * definition the server enforces - backend/app/services/password_policy.py).
 * The checklist is only a guide: the server checks everything again on
 * save, including the list of common passwords, which is not sent to the
 * browser (that rule shows "checked when you save").
 */
import { api } from "./api.js";

let policyPromise = null;

export function loadPolicy() {
  if (!policyPromise) {
    policyPromise = api.passwordPolicy().catch(() => ({
      min_length: 8, max_length: 128, guide_url: "/guide.html#password-policy",
      rules: [
        { id: "length", text: "8 to 128 characters" }, { id: "upper", text: "An uppercase letter (A-Z)" },
        { id: "lower", text: "A lowercase letter (a-z)" }, { id: "digit", text: "A digit (0-9)" },
        { id: "symbol", text: "A symbol, e.g. ! @ # $ % or a space" },
        { id: "username", text: "Does not contain your username" }, { id: "common", text: "Not a common password" },
        { id: "different", text: "Different from your current password" },
      ],
    }));
  }
  return policyPromise;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** The fields' markup. `withCurrent`: include "Current password" (changing your own password). */
export function passwordFieldsHtml({ withCurrent, newLabel = "New password" }) {
  return `
    ${withCurrent ? `<label>Current password
      <input type="password" name="current_password" autocomplete="current-password" required></label>` : ""}
    <label class="pw-new-label">
      <span class="pw-label-text">${newLabel}
        <span class="info-tip">
          <button type="button" class="info-btn" aria-label="Password policy">i</button>
          <span class="info-panel" role="tooltip">
            <strong>Password policy</strong>
            <span class="info-panel-rules"></span>
            <a class="info-panel-link" href="/guide.html#password-policy" target="_blank" rel="noopener">Read the full password policy</a>
          </span>
        </span>
      </span>
      <input type="password" name="new_password" autocomplete="new-password" required>
    </label>
    <ul class="pw-checklist" aria-live="polite"></ul>
    <label>Confirm ${newLabel.toLowerCase()}
      <input type="password" name="confirm_password" autocomplete="new-password" required></label>
    <p class="pw-match" aria-live="polite"></p>
    <label class="checkbox"><input type="checkbox" class="pw-show"> Show passwords</label>`;
}

/**
 * Wire the fields inside `form`. `username()` returns the account's
 * username (for the "does not contain your username" rule). Returns
 * { reset(), ready() }: ready() is true when every rule the browser can
 * check passes and the confirmation matches.
 */
export async function wirePasswordFields(form, { username }) {
  const policy = await loadPolicy();
  const current = form.querySelector('input[name="current_password"]');
  const next = form.querySelector('input[name="new_password"]');
  const confirm = form.querySelector('input[name="confirm_password"]');
  const list = form.querySelector(".pw-checklist");
  const match = form.querySelector(".pw-match");
  const rules = policy.rules.filter((rule) => rule.id !== "different" || current);

  form.querySelector(".info-panel-rules").innerHTML =
    `<ul>${rules.map((rule) => `<li>${escapeHtml(rule.text)}</li>`).join("")}</ul>`;
  form.querySelector(".info-panel-link").href = policy.guide_url;

  const checks = {
    length: (p) => p.length >= policy.min_length && p.length <= policy.max_length,
    upper: (p) => /\p{Lu}/u.test(p),
    lower: (p) => /\p{Ll}/u.test(p),
    digit: (p) => /\p{Nd}/u.test(p),
    symbol: (p) => /[^\p{L}\p{N}]/u.test(p),
    username: (p) => { const u = (username() || "").trim().toLowerCase(); return !u || !p.toLowerCase().includes(u); },
    common: () => null,                                    // the server checks it on save
    different: (p) => !current || p !== current.value,
  };

  function update() {
    const value = next.value;
    let allOk = true;
    list.innerHTML = rules.map((rule) => {
      const result = value ? checks[rule.id](value) : false;
      if (result === false) allOk = false;
      const state = result === null ? "pending" : result ? "ok" : "bad";
      const note = result === null ? " <span class=\"hint\">(checked when you save)</span>" : "";
      return `<li class="pw-${state}"><span class="pw-mark" aria-hidden="true"></span>${escapeHtml(rule.text)}${note}`
        + `<span class="sr-only">${state === "ok" ? " - met" : state === "bad" ? " - not met" : ""}</span></li>`;
    }).join("");
    const matches = confirm.value !== "" && confirm.value === value;
    match.textContent = confirm.value === "" ? "" : matches ? "The passwords match." : "The passwords do not match yet.";
    match.className = `pw-match ${confirm.value === "" ? "" : matches ? "pw-ok" : "pw-bad"}`;
    return allOk && matches;
  }

  [current, next, confirm].filter(Boolean).forEach((input) => input.addEventListener("input", update));
  form.querySelector(".pw-show").addEventListener("change", (e) => {
    form.querySelectorAll('input[name$="password"]').forEach((input) => { input.type = e.target.checked ? "text" : "password"; });
  });
  update();
  return {
    ready: update,
    reset() {
      form.querySelectorAll('input[name$="password"]').forEach((input) => { input.value = ""; input.type = "password"; });
      form.querySelector(".pw-show").checked = false;
      update();
    },
  };
}

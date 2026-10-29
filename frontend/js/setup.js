/**
 * The first-start setup page (setup.html; backend/app/api/setup.py).
 *
 * Steps: setup code -> recovery key (shown once; confirmed by typing its last
 * two groups) -> first admin account -> optional AI settings -> finish. All
 * answers stay in this page until "Finish setup", which sends them in one
 * request; the server checks everything again before it saves anything.
 */
import { api } from "./api.js";
import { passwordFieldsHtml, wirePasswordFields } from "./password-fields.js";

const answers = { code: "", recoveryCheck: "", admin: null };
let info = null;

const $ = (selector, root = document) => root.querySelector(selector);
const fill = (name, text) => { const el = $(`[data-fill="${name}"]`); el.textContent = text; return el; };

function show(step) {
  document.querySelectorAll("[data-panel]").forEach((panel) => { panel.hidden = panel.dataset.panel !== step; });
  let reached = true;
  document.querySelectorAll(".setup-steps li").forEach((item) => {
    item.classList.toggle("done", reached && item.dataset.step !== step);
    item.toggleAttribute("aria-current", item.dataset.step === step);
    if (item.dataset.step === step) reached = false;
  });
  const first = $(`[data-panel="${step}"] input, [data-panel="${step}"] select`);
  if (first) first.focus();
}

function failWith(form, err) {
  const el = $(".form-error", form);
  el.textContent = err.message;
  el.hidden = false;
}

// --- 1. setup code --------------------------------------------------------------------------
$("#code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  $(".form-error", form).hidden = true;
  try {
    info = await api.setupStart(form.code.value.trim());
    answers.code = form.code.value.trim();
  } catch (err) {
    failWith(form, err);
    return;
  }
  fill("key-store", info.key_store.description || "this computer's key store");
  if (info.key_store.warning) fill("key-store-warning", `Note: ${info.key_store.warning}`).hidden = false;
  prepareAI();
  if (info.keys_exist) {                 // a setup that was interrupted after the keys were saved
    show("admin");
    return;
  }
  fill("recovery-key", info.recovery_key);
  fill("fingerprint", info.fingerprint);
  show("recovery");
});

// --- 2. recovery key ------------------------------------------------------------------------
$('[data-action="copy"]').addEventListener("click", async (e) => {
  try {
    await navigator.clipboard.writeText(info.recovery_key);
    e.target.textContent = "Copied";
  } catch {
    e.target.textContent = "Select and copy it by hand";
  }
});
$('[data-action="download"]').addEventListener("click", () => {
  const text = `Incident Bridge recovery key\n\n${info.recovery_key}\n\nKey fingerprint: ${info.fingerprint}\n`
    + `Created: ${new Date().toISOString()}\n\nKeep this offline and away from the server. It opens the encrypted `
    + `database on any computer: python manage_keys.py restore\n`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  link.download = `incident-bridge-recovery-key-${info.fingerprint}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
});
$("#recovery-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  const expected = info.recovery_key.split("-").slice(-2).join("");
  const typed = form.check.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (typed !== expected) {
    failWith(form, new Error("That does not match the last two groups of the recovery key."));
    return;
  }
  answers.recoveryCheck = form.check.value;
  show("admin");
});

// --- 3. admin account -----------------------------------------------------------------------
const adminForm = $("#admin-form");
$(".pw-fields", adminForm).innerHTML = passwordFieldsHtml({ withCurrent: false, newLabel: "Password" });
const adminFields = await wirePasswordFields(adminForm, { username: () => adminForm.username.value });
adminForm.addEventListener("submit", (e) => {
  e.preventDefault();
  $(".form-error", adminForm).hidden = true;
  if (!adminFields.ready()) {
    failWith(adminForm, new Error("The password does not meet the policy yet, or the two do not match."));
    return;
  }
  answers.admin = { username: adminForm.username.value.trim(), password: adminForm.new_password.value,
                    confirm: adminForm.confirm_password.value };
  show("ai");
});

// --- 4. AI analysis and finish --------------------------------------------------------------
const aiForm = $("#ai-form");

function prepareAI() {
  aiForm.provider.innerHTML = `<option value="none">No AI analysis for now</option>`
    + info.providers.map((p) => `<option value="${p.id}">${p.name}${p.id === "fake" ? " (canned test answers)" : ""}</option>`).join("");
  if (info.ai_from_env) {
    fill("ai-env", `Your .env already sets up ${info.ai_from_env.provider}; with "No AI analysis for now" that `
      + `setting is used. Anything chosen here is used instead.`).hidden = false;
  }
  const users = info.users_problem
    ? `INCIDENT_BRIDGE_USERS has a problem - fix it in .env and restart the server:\n${info.users_problem}`
    : info.regular_users.length
      ? `These regular users from INCIDENT_BRIDGE_USERS will be created too: ${info.regular_users.join(", ")}.`
      : "No regular users are listed in INCIDENT_BRIDGE_USERS - add them later on the Users page.";
  fill("users", users).classList.toggle("warn", Boolean(info.users_problem));
  updateAIFields();
}

function aiChoice() {
  const provider = aiForm.provider.value;
  if (provider === "none") return null;
  return { provider, api_key: aiForm.api_key.value, models: aiForm.models.value,
           gemini_tier: provider === "gemini" ? aiForm.gemini_tier.value : null };
}

function updateAIFields() {
  const chosen = info.providers.find((p) => p.id === aiForm.provider.value);
  $("[data-ai-fields]").hidden = !chosen;
  if (!chosen) return;
  $("[data-ai-key]").hidden = !chosen.needs_key;
  $("[data-ai-models]").hidden = !chosen.models_setting;
  $("[data-ai-tier]").hidden = chosen.id !== "gemini";
  fill("ai-result", "");
}
aiForm.provider.addEventListener("change", updateAIFields);

$('[data-action="test-ai"]').addEventListener("click", async (e) => {
  e.target.disabled = true;
  fill("ai-result", "Testing…");
  try {
    const result = await api.setupTestAI(answers.code, aiChoice());
    fill("ai-result", result.detail);
  } catch (err) {
    fill("ai-result", err.message);
  } finally {
    e.target.disabled = false;
  }
});

aiForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $(".form-error", aiForm).hidden = true;
  const button = $('button[type="submit"]', aiForm);
  button.disabled = true;
  try {
    const result = await api.setupComplete({
      code: answers.code, recovery_check: answers.recoveryCheck, admin_username: answers.admin.username,
      admin_password: answers.admin.password, confirm_password: answers.admin.confirm, ai: aiChoice(),
    });
    const users = result.regular_users.length ? ` Regular users created: ${result.regular_users.join(", ")}.` : "";
    fill("done-text", `Log in as ${result.admin}.${users}`);
    if (result.key_store_warning) fill("done-warning", `Note: ${result.key_store_warning}`).hidden = false;
    show("done");
  } catch (err) {
    failWith(aiForm, err);
  } finally {
    button.disabled = false;
  }
});

show("code");

/**
 * Server settings page (admin only): the app's settings and HTTPS (saved
 * encrypted, applied at the next restart), the AI analysis settings (applied
 * at once) and the data imports - through /server, /incidents and /sites
 * (backend/app/api/), which check everything again.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { showToast } from "./toast.js";
import { escapeHtml } from "./sites-format.js";

// Declared before the top-level await below runs the page code that uses them.
const configForm = document.getElementById("config-form");
const httpsForm = document.getElementById("https-form");
const aiForm = document.getElementById("ai-form");
const SOURCE_LABELS = { saved: "saved", default: "default" };
const IMPORTS = {
  incidents: { send: (content) => api.importJsonl(content), word: ["incident", "incidents"] },
  sites: { send: (content) => api.importSites(content), word: ["site", "sites"] },
  lists: { send: (content) => api.importMailingLists(content), word: ["mailing list", "mailing lists"] },
};
let restart = { config: false, https: false };
let report = null;     // GET /server/settings
let aiInfo = null;     // GET /server/ai

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  document.getElementById("server-page").hidden = false;
  wireTabs();
  wireConfig();
  wireHttps();
  wireAI();
  wireImports();
  showConfig(await api.serverConfig());
  showSettings(await api.serverSettings());
  showAI(await api.serverAI());
  if (location.hash) window.scrollTo(0, 0);           // opened at a tab's link: keep the tabs in view
}

// --- tabs: real links (#settings, #https, #ai, #import), so a tab can be opened or bookmarked -------

function showTab() {
  const wanted = location.hash.slice(1);
  const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];
  const current = tabs.find((tab) => tab.id === `tab-${wanted}`) || tabs[0];
  tabs.forEach((tab) => {
    const selected = tab === current;
    tab.setAttribute("aria-selected", String(selected));
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
  });
}

function wireTabs() {
  document.querySelectorAll('.tabs [role="tab"]').forEach((tab) => tab.addEventListener("click", (e) => {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;     // a new tab or window: the browser's
    e.preventDefault();                                                    // no jump down to the panel
    history.pushState(null, "", tab.getAttribute("href"));
    showTab();
  }));
  window.addEventListener("popstate", showTab);
  showTab();
}

function showRestartNote() {
  document.getElementById("restart-note").hidden = !(restart.config || restart.https);
}

// --- the general settings -----------------------------------------------------------------------

function configField(s) {
  const id = `config-${s.name}`;
  const tag = `<span class="source-tag" data-kind="${s.saved ? "saved" : "default"}">${s.saved ? "saved" : "default"}</span>`;
  const help = s.help ? `<span class="hint field-hint">${escapeHtml(s.help)}</span>` : "";
  const value = s.value ?? "";
  if (s.kind === "bool") {
    const on = (s.saved ? value : s.default) === "true";
    return `<label class="checkbox"><input type="checkbox" id="${id}" name="${s.name}"${on ? " checked" : ""}>
      ${escapeHtml(s.label)} ${tag}</label>${help}`;
  }
  if (s.kind === "choice") {
    const options = s.choices.map((c) => `<option value="${escapeHtml(c)}"${(s.saved ? value : s.default) === c
      ? " selected" : ""}>${escapeHtml(c)}${c === s.default ? " (default)" : ""}</option>`).join("");
    return `<label for="${id}">${escapeHtml(s.label)} ${tag}${help}<select id="${id}" name="${s.name}">${options}</select></label>`;
  }
  const type = s.kind === "number" ? ' type="number" min="0" step="any"' : ' type="text"';
  return `<label for="${id}">${escapeHtml(s.label)} ${tag}${help}<input${type} id="${id}" name="${s.name}"
    value="${escapeHtml(value)}" placeholder="${escapeHtml(s.default)} (default)" autocomplete="off"></label>`;
}

function showConfig(data) {
  configForm.dataset.settings = JSON.stringify(data.groups.flatMap((g) => g.settings));
  document.getElementById("config-groups").innerHTML = data.groups.map((g) => `
    <fieldset class="config-group"><legend>${escapeHtml(g.name)}</legend>
      ${g.settings.map(configField).join("")}</fieldset>`).join("");
  restart.config = data.restart_needed;
  showRestartNote();
}

function configChanges() {
  // Only what differs from what is saved (or from the default, when nothing is) - the rest keeps its source.
  const changes = {};
  JSON.parse(configForm.dataset.settings).forEach((s) => {
    const field = configForm.elements[s.name];
    const value = s.kind === "bool" ? String(field.checked) : field.value.trim();
    const current = s.saved ? s.value : (s.kind === "text" || s.kind === "number" ? "" : s.default);
    if (value !== current) changes[s.name] = value;
  });
  return changes;
}

function wireConfig() {
  const error = document.getElementById("config-error");
  configForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const changes = configChanges();
    if (!Object.keys(changes).length) {
      showToast("Nothing changed.");
      return;
    }
    try {
      const saved = await api.saveServerConfig(changes);
      showConfig(saved);
      showToast("Settings saved - restart the server to apply them.");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
}

// --- HTTPS --------------------------------------------------------------------------------------

function showSettings(data) {
  report = data;
  const s = data.settings;
  const tag = document.getElementById("mode-tag");
  tag.textContent = data.mode === "production" ? "Production" : "Testing";
  tag.className = `mode-tag mode-${data.mode}`;
  tag.title = data.mode === "production"
    ? "Production mode: HTTPS with a certificate from a certificate authority is required."
    : "Testing mode: the generated self-signed certificate is allowed (Settings > Server > Mode).";
  httpsForm.http_port.value = s.http_port;
  httpsForm.https_port.value = s.https_port;
  httpsForm.host.value = s.host;
  httpsForm.https_enabled.checked = s.https_enabled;
  httpsForm.https_only_remote.checked = s.https_only_remote;
  httpsForm.certificate.value = s.certificate;
  httpsForm.cert_file.value = s.cert_file;
  httpsForm.key_file.value = s.key_file;
  httpsForm.key_password.value = "";
  httpsForm.key_password.placeholder = s.key_password_set ? "saved - leave empty to keep it" : "";
  httpsForm.trusted_proxies.value = s.trusted_proxies.join(", ");
  httpsForm.querySelectorAll("[data-source]").forEach((el) => {
    const source = data.sources[el.dataset.source];
    el.textContent = SOURCE_LABELS[source] || "";
    el.dataset.kind = source;
  });
  const generate = httpsForm.querySelector('[data-action="generate-cert"]');
  generate.disabled = data.mode === "production";
  showRunning(data);
  showCertificate(data.certificate);
  updateHttpsFields();
  restart.https = data.restart_needed;
  showRestartNote();
}

function showRunning(data) {
  const r = data.running;
  const el = document.getElementById("running");
  if (!r.known) {
    el.textContent = "Running now: started without serve_app.py, so the listeners are unknown here.";
    return;
  }
  const where = location.hostname || "this-server";
  const parts = [`HTTP on port ${r.http_port}`];
  if (r.https_port) parts.push(`HTTPS on port ${r.https_port} (https://${where}:${r.https_port})`);
  else parts.push("no HTTPS");
  if (r.https_only_remote) parts.push("remote HTTP redirected to HTTPS");
  el.textContent = `Running now: ${parts.join(", ")}.`;
}

function showCertificate(cert) {
  const box = document.getElementById("cert-report");
  if (!cert) {
    box.innerHTML = "";
    return;
  }
  const lines = [];
  if (cert.info) {
    const i = cert.info;
    lines.push(`<dl class="cert-info">
      <dt>Issued to</dt><dd>${escapeHtml(i.subject)}</dd>
      <dt>Issued by</dt><dd>${escapeHtml(i.issuer)}${i.self_signed ? ' <span class="hint">self-signed</span>' : ""}</dd>
      <dt>Names</dt><dd>${i.names.map(escapeHtml).join(", ") || "—"}</dd>
      <dt>Valid until</dt><dd>${escapeHtml(i.not_after.slice(0, 10))} <span class="hint">${i.days_left} day(s) left</span></dd>
      <dt>Fingerprint</dt><dd><code class="fingerprint">${escapeHtml(i.fingerprint)}</code></dd></dl>`);
  }
  if (cert.note) lines.push(`<p class="hint">${escapeHtml(cert.note)}</p>`);
  (cert.problems || []).forEach((p) => lines.push(`<p class="setup-note warn">${escapeHtml(p)}</p>`));
  (cert.warnings || []).forEach((w) => lines.push(`<p class="setup-note">${escapeHtml(w)}</p>`));
  box.innerHTML = lines.join("");
}

function updateHttpsFields() {
  const on = httpsForm.https_enabled.checked;
  const files = httpsForm.certificate.value === "files";
  httpsForm.querySelector(".cert-fields").disabled = !on;
  httpsForm.https_only_remote.disabled = !on;
  httpsForm.querySelectorAll("[data-files]").forEach((el) => { el.hidden = !files; });
  httpsForm.querySelectorAll("[data-generated]").forEach((el) => { el.hidden = files; });
}

function httpsChanges() {
  const f = httpsForm;
  const body = {
    host: f.host.value.trim(), http_port: Number(f.http_port.value), https_port: Number(f.https_port.value),
    https_enabled: f.https_enabled.checked, https_only_remote: f.https_enabled.checked && f.https_only_remote.checked,
    certificate: f.certificate.value || "generated", cert_file: f.cert_file.value.trim(),
    key_file: f.key_file.value.trim(), trusted_proxies: f.trusted_proxies.value.trim(),
  };
  if (f.key_password.value) body.key_password = f.key_password.value;
  // Only what differs from the current values is saved, so the rest keeps its default.
  const current = { ...report.settings, trusted_proxies: report.settings.trusted_proxies.join(", ") };
  return Object.fromEntries(Object.entries(body).filter(([name, value]) =>
    name === "key_password" || report.sources[name] === "saved" || String(current[name]) !== String(value)));
}

function wireHttps() {
  const error = document.getElementById("https-error");
  httpsForm.addEventListener("change", updateHttpsFields);
  httpsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    try {
      showSettings(await api.saveServerSettings(httpsChanges()));
      showToast("Server settings saved - restart the server to apply them.");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  httpsForm.querySelector('[data-action="reset"]').addEventListener("click", async () => {
    error.hidden = true;
    try {
      showSettings(await api.resetServerSettings());
      showToast("The HTTPS settings are back to the defaults - from the next restart.");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  httpsForm.querySelector('[data-action="check-cert"]').addEventListener("click", async () => {
    error.hidden = true;
    try {
      const body = { cert_file: httpsForm.cert_file.value.trim(), key_file: httpsForm.key_file.value.trim() };
      if (httpsForm.key_password.value) body.key_password = httpsForm.key_password.value;
      showCertificate(await api.checkCertificate(body));
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  httpsForm.querySelector('[data-action="generate-cert"]').addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      await api.generateCertificate();
      showSettings(await api.serverSettings());
      showToast("A new self-signed certificate was made - it is used from the next restart.");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      e.target.disabled = report && report.mode === "production";
    }
  });
}

// --- AI analysis --------------------------------------------------------------------------------

function showAI(data) {
  aiInfo = data;
  aiForm.provider.innerHTML = `<option value="off">Off</option>`
    + data.providers.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}${
      p.id === "fake" ? " (canned test answers)" : ""}</option>`).join("");
  aiForm.provider.value = data.provider;
  aiForm.api_key.value = "";
  aiForm.models.value = data.models;
  aiForm.gemini_tier.value = data.gemini_tier || "free";
  document.getElementById("ai-status").textContent = `Now: ${data.status}`;
  updateAIFields();
}

function updateAIFields() {
  const chosen = aiInfo.providers.find((p) => p.id === aiForm.provider.value);
  aiForm.querySelectorAll("[data-ai-fields]").forEach((el) => { el.hidden = !chosen; });
  if (!chosen) return;
  aiForm.querySelector("[data-ai-key]").hidden = !chosen.needs_key;
  aiForm.querySelector("[data-ai-models]").hidden = !chosen.models_setting;
  aiForm.querySelector("[data-ai-tier]").hidden = chosen.id !== "gemini";
  const keeps = aiInfo.has_key && aiInfo.provider === chosen.id;
  aiForm.querySelector('[data-fill="ai-key-hint"]').textContent = !keeps ? ""
    : aiInfo.key_source === ".env" ? "from backend/.env - leave empty to keep using it" : "saved - leave empty to keep it";
}

function aiChoice() {
  return { provider: aiForm.provider.value, api_key: aiForm.api_key.value, models: aiForm.models.value,
           gemini_tier: aiForm.provider.value === "gemini" ? aiForm.gemini_tier.value : null };
}

function wireAI() {
  const error = document.getElementById("ai-error");
  const result = aiForm.querySelector('[data-fill="ai-result"]');
  aiForm.provider.addEventListener("change", () => { result.textContent = ""; updateAIFields(); });
  aiForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    try {
      const saved = await api.saveServerAI(aiChoice());
      showAI(saved);
      result.textContent = "";
      showToast(saved.applied ? "AI analysis settings saved and applied."
        : "AI analysis settings saved - an analysis is still running, so they apply at the next restart.");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
  aiForm.querySelector('[data-action="test-ai"]').addEventListener("click", async (e) => {
    e.target.disabled = true;
    error.hidden = true;
    result.textContent = "Testing…";
    try {
      result.textContent = (await api.testServerAI(aiChoice())).detail;
    } catch (err) {
      result.textContent = err.message;
    } finally {
      e.target.disabled = false;
    }
  });
}

// --- imports ------------------------------------------------------------------------------------

function importSummary(result, [one, many]) {
  const count = (n) => `${n} ${n === 1 ? one : many}`;
  const already = (result.already_stored_ids || []).length;
  const duplicates = result.skipped_duplicate_ids.length;
  const list = (items) => `<ul>${items.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`;
  return `<p><strong>${count(result.created)}</strong> imported.</p>`
    + (already ? `<p>${already} skipped (already stored).</p>` : "")
    + (duplicates ? `<p>${duplicates} skipped (the same id twice in the file).</p>` : "")
    + (result.skipped_invalid.length ? `<p>${result.skipped_invalid.length} skipped (invalid):</p>${list(result.skipped_invalid)}` : "")
    + ((result.warnings || []).length ? `<p>Warnings:</p>${list(result.warnings)}` : "");
}

function wireImports() {
  document.querySelectorAll(".import-form").forEach((form) => {
    const kind = IMPORTS[form.dataset.kind];
    const summary = form.querySelector(".import-summary");
    const button = form.querySelector('button[type="submit"]');
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const file = form.file.files[0];
      if (!file) return;
      button.disabled = true;
      summary.hidden = true;
      try {
        // The file's own text is sent, not a path - the server cannot open a path on this computer.
        summary.innerHTML = importSummary(await kind.send(await file.text()), kind.word);
      } catch (err) {
        summary.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
      } finally {
        summary.hidden = false;
        button.disabled = false;
      }
    });
  });
}

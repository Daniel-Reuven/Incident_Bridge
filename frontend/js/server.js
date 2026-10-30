/**
 * Server settings page (admin only): HTTPS (saved, applied at the next
 * restart) and the AI analysis settings (applied at once) - through /server
 * (backend/app/api/server.py), which checks everything again.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { showToast } from "./toast.js";
import { escapeHtml } from "./sites-format.js";

// Declared before the top-level await below runs the page code that uses them.
const httpsForm = document.getElementById("https-form");
const aiForm = document.getElementById("ai-form");
const SOURCE_LABELS = { page: "set here", ".env": "from .env", default: "default" };
let report = null;     // GET /server/settings
let aiInfo = null;     // GET /server/ai

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  document.getElementById("server-page").hidden = false;
  wireHttps();
  wireAI();
  showSettings(await api.serverSettings());
  showAI(await api.serverAI());
}

// --- HTTPS --------------------------------------------------------------------------------------

function showSettings(data) {
  report = data;
  const s = data.settings;
  const tag = document.getElementById("mode-tag");
  tag.textContent = data.mode === "production" ? "Production" : "Testing";
  tag.className = `mode-tag mode-${data.mode}`;
  tag.title = data.mode === "production"
    ? "APP_MODE=production: HTTPS with a certificate from a certificate authority is required."
    : "APP_MODE=testing (backend/.env): the generated self-signed certificate is allowed.";
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
  document.getElementById("restart-note").hidden = !data.restart_needed;
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
  // Only what differs from the current values is saved here, so the rest keeps following .env.
  const current = { ...report.settings, trusted_proxies: report.settings.trusted_proxies.join(", ") };
  return Object.fromEntries(Object.entries(body).filter(([name, value]) =>
    name === "key_password" || report.sources[name] === "page" || String(current[name]) !== String(value)));
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
    showSettings(await api.resetServerSettings());
    showToast("The page's server settings were removed - .env applies at the next restart.");
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
  aiForm.provider.innerHTML = `<option value="">Use the .env settings</option><option value="off">Off</option>`
    + data.providers.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}${
      p.id === "fake" ? " (canned test answers)" : ""}</option>`).join("");
  aiForm.provider.value = data.provider;
  aiForm.api_key.value = "";
  aiForm.models.value = data.models;
  aiForm.gemini_tier.value = data.gemini_tier || "free";
  document.getElementById("ai-status").textContent = `Now: ${data.status}${data.source === "page" ? " (set here)" : " (from .env)"}`;
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
  aiForm.querySelector('[data-fill="ai-key-hint"]').textContent = keeps ? "saved - leave empty to keep it" : "";
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

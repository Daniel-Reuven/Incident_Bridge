/**
 * Activity log page (admin only): one month of records from GET /logs,
 * filtered on the server, newest first, 200 at a time ("Show older records"
 * loads the next 200). The Category / Event / User menus list what occurs
 * in the chosen month, with counts (the response's "facets"). Clicking a
 * row shows every field of that record. The chosen filters are kept for the
 * browser tab's session.
 *
 * "Check integrity" asks the server to re-check every record's signature and
 * the chain against the checkpoint in the database (GET /logs/verify,
 * backend/app/audit_integrity.py) and shows the result above the filters.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { formatWhen } from "./datetime.js";

const PAGE = 200;
const FILTERS = ["f-month", "f-category", "f-event", "f-user", "f-level", "f-text"];
const PARAM = { "f-month": "month", "f-category": "category", "f-event": "event", "f-user": "user",
                "f-level": "level", "f-text": "q" };
let loaded = [];
let lastQuery = null;

const user = await mountTopbar();
if (user && user.role !== "admin") {
  window.location.href = "/dashboard.html";
} else if (user) {
  document.getElementById("logs-page").hidden = false;
  await initMonths();
  setDefaultCategory();
  restoreFilters();
  wire();
  await load();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function remember() {
  try {
    sessionStorage.setItem("logs.filters", JSON.stringify(Object.fromEntries(
      FILTERS.map((id) => [id, document.getElementById(id).value]))));
  } catch { /* not kept - fine */ }
}

function restoreFilters() {
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem("logs.filters") || "{}"); } catch { /* defaults */ }
  for (const id of FILTERS) {
    const el = document.getElementById(id);
    if (saved[id] === undefined) continue;
    if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === saved[id])) {
      el.add(new Option(saved[id], saved[id]));          // a facet option, filled for real after loading
    }
    el.value = saved[id];
  }
}

function setDefaultCategory() {
  const select = document.getElementById("f-category");
  if (!select.options.length) select.add(new Option("All except HTTP requests", "!http"));
  select.value = "!http";
}

async function initMonths() {
  const months = await api.logMonths();
  const select = document.getElementById("f-month");
  select.innerHTML = months.length
    ? months.map((m) => `<option value="${m.month}">${m.month}${m.archived ? " (archived)" : ""}</option>`).join("")
    : `<option value="">No records yet</option>`;
}

function params(offset) {
  const out = { limit: PAGE, offset };
  for (const id of FILTERS) out[PARAM[id]] = document.getElementById(id).value.trim();
  return out;
}

function fillFacet(id, counts, allLabel, extraOptions = "") {
  const select = document.getElementById(id);
  const current = select.value;
  const names = Object.keys(counts || {});
  if (current && !current.startsWith("!") && !names.includes(current)) names.push(current);
  select.innerHTML = extraOptions + `<option value="">${allLabel}</option>` + names.sort().map((name) =>
    `<option value="${escapeHtml(name)}">${escapeHtml(name)}${counts[name] ? ` (${counts[name]})` : ""}</option>`).join("");
  select.value = current;
}

async function load(append = false) {
  const query = params(append ? loaded.length : 0);
  lastQuery = query;
  let body;
  try {
    body = await api.readLog(query);
  } catch (err) {
    document.getElementById("log-summary").textContent = err.message;
    return;
  }
  if (lastQuery !== query) return;                      // a newer request was made meanwhile
  const category = document.getElementById("f-category").value;
  const events = Object.fromEntries(Object.entries(body.facets.event)
    .filter(([name]) => !category || (category.startsWith("!") ? !name.startsWith(`${category.slice(1)}.`)
                                                               : name.startsWith(`${category}.`))));
  // Default: everything except the HTTP requests, which outnumber the actions many times over.
  fillFacet("f-category", body.facets.category, "All, with HTTP requests",
            `<option value="!http">All except HTTP requests</option>`);
  fillFacet("f-event", events, "All");
  fillFacet("f-user", body.facets.user, "Anyone");

  loaded = append ? loaded.concat(body.entries) : body.entries;
  render(body);
}

function render(body) {
  const summary = body.month
    ? `${body.total.toLocaleString()} record${body.total === 1 ? "" : "s"} match in ${body.month}`
      + (body.total > loaded.length ? ` - showing the newest ${loaded.length.toLocaleString()}` : "")
      + (body.unreadable ? ` (${body.unreadable} unreadable line${body.unreadable === 1 ? "" : "s"} skipped)` : "")
    : "No activity has been recorded yet.";
  document.getElementById("log-summary").textContent = summary;
  document.getElementById("more-btn").hidden = body.total <= loaded.length;
  document.getElementById("log-body").innerHTML = loaded.length
    ? loaded.map((r, i) => `
      <tr class="log-row log-${escapeHtml((r.level || "").toLowerCase())}" data-index="${i}" tabindex="0">
        <td class="log-time">${escapeHtml(formatWhen(r.ts))}</td>
        <td class="log-level">${escapeHtml(r.level)}</td>
        <td class="log-event">${escapeHtml(r.event)}</td>
        <td>${escapeHtml(r.user || "—")}</td>
        <td class="log-ip">${escapeHtml(r.ip || "")}</td>
        <td class="log-message">${escapeHtml(r.message)}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="empty">No records match these filters.</td></tr>`;
}

function toggleDetails(row) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("log-details")) {
    next.remove();
    row.setAttribute("aria-expanded", "false");
    return;
  }
  const record = loaded[Number(row.dataset.index)];
  const details = document.createElement("tr");
  details.className = "log-details";
  details.innerHTML = `<td colspan="6"><pre>${escapeHtml(JSON.stringify(record, null, 2))}</pre></td>`;
  row.after(details);
  row.setAttribute("aria-expanded", "true");
}

function wire() {
  let timer = null;
  for (const id of FILTERS) {
    const el = document.getElementById(id);
    el.addEventListener(el.tagName === "INPUT" ? "input" : "change", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (id === "f-month" || id === "f-category") document.getElementById("f-event").value = "";
        remember();
        load();
      }, el.tagName === "INPUT" ? 300 : 0);
    });
  }
  document.getElementById("refresh-btn").addEventListener("click", async () => {
    const month = document.getElementById("f-month").value;
    await initMonths();
    document.getElementById("f-month").value = month || document.getElementById("f-month").value;
    load();
  });
  document.getElementById("more-btn").addEventListener("click", () => load(true));
  document.getElementById("verify-btn").addEventListener("click", checkIntegrity);
  const tbody = document.getElementById("log-body");
  tbody.addEventListener("click", (e) => {
    const row = e.target.closest("tr.log-row");
    if (row) toggleDetails(row);
  });
  tbody.addEventListener("keydown", (e) => {
    const row = e.target.closest("tr.log-row");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleDetails(row); }
  });
}

async function checkIntegrity() {
  const box = document.getElementById("log-integrity");
  const button = document.getElementById("verify-btn");
  button.disabled = true;
  box.hidden = false;
  box.className = "log-integrity";
  box.textContent = "Checking every record…";
  try {
    const result = await api.verifyLog();
    if (result.ok) {
      box.classList.add("log-integrity-ok");
      box.innerHTML = `<strong>No tampering found.</strong> ${result.records.toLocaleString()} records checked: every `
        + `signature matches and the chain is complete up to record ${result.last_seq.toLocaleString()} `
        + `(the checkpoint in the database).`;
    } else {
      box.classList.add("log-integrity-bad");
      const items = result.problems.map((p) =>
        `<li>${p.month ? `${escapeHtml(p.month)}, ` : ""}${p.seq != null ? `record ${p.seq}: ` : ""}${escapeHtml(p.problem)}</li>`).join("");
      const more = result.problem_count > result.problems.length
        ? `<p>…and ${result.problem_count - result.problems.length} more.</p>` : "";
      box.innerHTML = `<strong>The log was changed after it was written.</strong> ${result.problem_count} `
        + `problem${result.problem_count === 1 ? "" : "s"} in ${result.records.toLocaleString()} records:<ul>${items}</ul>${more}`;
    }
  } catch (err) {
    box.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

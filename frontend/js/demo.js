/**
 * The requirements demo page (demo.html, any logged-in user): one section per
 * requirement from GET /demo/requirements - its summary, a Run button
 * (POST /demo/requirements/{id}/run: the demo runs on the server on fresh
 * in-memory data and its output lines come back), the demo's code, the
 * app's code that meets the requirement and where the app uses it.
 * Everything from the server is shown as text, never as HTML.
 */
import { api } from "./api.js";
import { mountTopbar } from "./topbar.js";
import { mountDocNavigation } from "./doc-nav.js";

// Declared before the top-level await below runs the page code that uses them.
const list = document.getElementById("demo-list");
const toc = document.getElementById("demo-toc");

const user = await mountTopbar();
if (user) {
  const data = await api.demoRequirements();
  data.requirements.forEach((requirement, index) => list.append(section(requirement, index + 1)));
  document.getElementById("demo-note").textContent = data.note;
  document.getElementById("demo-page").hidden = false;
  mountDocNavigation(document.querySelector(".doc-toc[data-built-later]"));
  if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === "text") node.textContent = value;
    else node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

function codeBlock(title, source) {
  const details = el("details", { class: "demo-code" });
  details.append(el("summary", { text: title }), el("pre", {}, el("code", { text: source })));
  return details;
}

function section(requirement, number) {
  const id = `req-${requirement.id}`;
  toc.append(el("li", {}, el("a", { href: `#${id}`, text: requirement.title })));

  const box = el("section", { class: "demo-item", "aria-labelledby": id });
  box.append(el("h2", { id }, el("span", { class: "demo-number", text: `${number}.` }), " ", requirement.title));
  box.append(el("p", { text: requirement.brief }));

  const output = el("pre", { class: "demo-output", hidden: "" });
  const status = el("span", { class: "hint demo-status", role: "status" });
  const run = el("button", { type: "button", class: "btn btn-primary btn-sm", text: "Run" });
  run.addEventListener("click", async () => {
    run.disabled = true;
    status.textContent = "Running…";
    try {
      const result = await api.runDemo(requirement.id);
      output.textContent = result.output.join("\n") + (result.error ? `\n\n${result.error}` : "");
      output.classList.toggle("failed", Boolean(result.error));
      status.textContent = result.error ? "The demo stopped with an error."
        : `Done in ${result.ms < 1 ? "under 1" : result.ms} ms.`;
      run.textContent = "Run again";
    } catch (err) {
      output.textContent = err.message;
      output.classList.add("failed");
      status.textContent = "";
    } finally {
      output.hidden = false;
      run.disabled = false;
    }
  });
  box.append(el("div", { class: "demo-run" }, run, status), output);

  box.append(codeBlock(`The demo — ${requirement.demo.file}`, requirement.demo.source));
  for (const code of requirement.code) {
    box.append(codeBlock(`${code.ref} — ${code.file}, line ${code.line}`, code.source));
  }

  const uses = el("ul", { class: "demo-uses" });
  for (const use of requirement.uses) {
    const item = el("li", { text: use.text });
    if (use.link) item.append(" ", el("a", { href: use.link, text: "Open" }));
    uses.append(item);
  }
  box.append(el("h3", { text: "Where the app uses it" }), uses);
  return box;
}

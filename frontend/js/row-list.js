/**
 * Keeps a container's row elements in sync with `items` without ever
 * doing `container.innerHTML = items.map(...).join("")` - that approach
 * destroys and recreates every row on every refresh, which is exactly
 * what causes visible flicker/flash when live updates trigger frequent
 * re-renders.
 *
 * Instead: existing rows are matched by key and left completely alone if
 * their content hasn't changed (via a cheap "sig" string comparison), a
 * changed row has just its innerHTML patched (the row element itself is
 * reused, not replaced), and unchanged rows are only ever moved (not
 * recreated) if their order changed.
 *
 * Click handling is NOT done per-row here on purpose - callers should use
 * one delegated listener on the container (see dashboard.js) so rows
 * never need their listeners re-attached after a patch.
 */

/**
 * @param container - the element whose children are the rows
 * @param items - the current list of data items
 * @param keyFn - item -> stable string id
 * @param renderFn - item -> { className, html, sig }
 * @param emptyMessage - shown when items is empty
 */
export function renderRowList(container, items, keyFn, renderFn, emptyMessage) {
  if (items.length === 0) {
    if (container.dataset.empty !== "1") {
      container.innerHTML = `<p class="empty">${emptyMessage}</p>`;
      container.dataset.empty = "1";
    }
    return;
  }
  if (container.dataset.empty === "1") {
    container.innerHTML = "";
  }
  container.dataset.empty = "0";

  const existing = new Map();
  container.querySelectorAll(":scope > [data-key]").forEach((el) => existing.set(el.dataset.key, el));

  let previous = null;
  const usedKeys = new Set();

  for (const item of items) {
    const key = String(keyFn(item));
    usedKeys.add(key);
    const { className, html, sig } = renderFn(item);

    let el = existing.get(key);
    if (!el) {
      el = document.createElement("div");
      el.dataset.key = key;
    }
    if (el.dataset.sig !== sig) {
      el.className = className;
      el.innerHTML = html;
      el.dataset.sig = sig;
    }
    // Keep DOM order matching item order - .after() on an already-correctly-
    // positioned node is a cheap no-op in the browser, not a reflow-causing move.
    if (previous) previous.after(el);
    else container.prepend(el);
    previous = el;
  }

  existing.forEach((el, key) => {
    if (!usedKeys.has(key)) el.remove();
  });
}

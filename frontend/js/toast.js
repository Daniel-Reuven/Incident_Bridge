/**
 * Toast notifications for live-update events ("tech1 commented on
 * Payment API is down", etc). Purely additive to the DOM - never touches
 * or re-renders anything else on the page, so it can't cause flicker
 * elsewhere.
 */

let stack;

function ensureStack() {
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "toast-stack";
    stack.setAttribute("aria-live", "polite");
    document.body.appendChild(stack);
  }
  return stack;
}

export function showToast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  ensureStack().appendChild(el);

  // Added in the next frame so the initial state (opacity/transform in
  // CSS) actually transitions in, instead of jumping straight to visible.
  requestAnimationFrame(() => el.classList.add("toast-visible"));

  setTimeout(() => {
    el.classList.remove("toast-visible");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
  }, 4500);
}

/**
 * Turns a live-update event payload (see events.js) into a human-readable
 * toast message, e.g.
 *   admin closed maintenance task "Patch server" (3 other tasks moved in the queue)
 * The title is quoted and the incident's kind named so it is clear what
 * changed. An event may carry affected_ids (the OTHER tasks whose queue
 * position shifted because of this action - see backend/app/api/incidents.py's
 * _publish); when it does, the message says how many.
 */
export function describeEvent(event) {
  const kind = event.type === "fault" ? "fault" : "maintenance task";
  const subject = `${kind} "${event.title}"`;
  const moved = Array.isArray(event.affected_ids) ? event.affected_ids.length : 0;
  const shifted = moved ? ` (${moved} other task${moved === 1 ? "" : "s"} moved in the queue)` : "";

  switch (event.action) {
    case "created":
      return `${event.actor} opened a new ${subject}`;
    case "commented":
      return `${event.actor} commented on ${subject}`;
    case "claimed":
      return `${event.actor} claimed ${subject}`;
    case "started":
      return `${event.actor} started ${subject}`;
    case "closed":
      return `${event.actor} closed ${subject}${shifted}`;
    case "reopened":
      return `${event.actor} reopened ${subject}${shifted}`;
    case "severity_changed":
      return `${event.actor} changed the severity of ${subject}`;
    default:
      return `${subject} was updated`;
  }
}
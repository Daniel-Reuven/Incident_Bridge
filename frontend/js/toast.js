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

/** Turns a live-update event payload (see events.js) into a human-readable toast message. */
export function describeEvent(event) {
  const kind = event.type === "fault" ? "fault" : "maintenance task";
  switch (event.action) {
    case "created":
      return `${event.actor} opened a new ${kind}: ${event.title}`;
    case "commented":
      return `${event.actor} commented on ${event.title}`;
    case "claimed":
      return `${event.actor} claimed ${event.title}`;
    case "started":
      return `${event.actor} started ${event.title}`;
    case "closed":
      return `${event.actor} closed ${event.title}`;
    case "severity_changed":
      return `${event.actor} changed the severity of ${event.title}`;
    default:
      return `${event.title} was updated`;
  }
}

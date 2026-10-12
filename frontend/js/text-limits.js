// Text-length limits for free-text inputs, plus a small live character
// counter shown under each limited textarea.
//
// These numbers MIRROR the backend, which is the real authority (it rejects
// anything longer with HTTP 400):
//   MAX_COMMENT_LENGTH -> backend/app/models/comment.py   (MAX_COMMENT_LENGTH)
//   MAX_REASON_LENGTH  -> backend/app/models/incident.py  (MAX_REASON_LENGTH)
// Change both sides together.
//
// How to use:
//   1. Give a textarea a maxlength from these constants, e.g.
//        <textarea name="text" maxlength="${MAX_COMMENT_LENGTH}"></textarea>
//   2. After the markup is in the page, call wireCharCounters(container).
//      Every textarea[maxlength] inside `container` gets a "12 / 5000"
//      counter right after it, updated as the user types.

export const MAX_COMMENT_LENGTH = 5000;
export const MAX_REASON_LENGTH = 1000;

/** Add a live "used / max" counter after every textarea[maxlength] inside `container`. */
export function wireCharCounters(container) {
  container.querySelectorAll("textarea[maxlength]").forEach((textarea) => {
    const counter = document.createElement("small");
    counter.className = "char-counter";
    textarea.insertAdjacentElement("afterend", counter);

    const max = Number(textarea.getAttribute("maxlength"));
    const update = () => {
      counter.textContent = `${textarea.value.length} / ${max}`;
      // Turns the counter amber in the last 10% so the limit never comes as a surprise.
      counter.classList.toggle("near-limit", textarea.value.length >= max * 0.9);
    };
    textarea.addEventListener("input", update);
    // Also refresh on form reset (e.g. a dialog form cleared after submit).
    textarea.form?.addEventListener("reset", () => setTimeout(update));
    update();
  });
}

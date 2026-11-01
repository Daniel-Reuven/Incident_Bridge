/**
 * Navigation for the long pages that have an "On this page" list
 * (<nav class="doc-toc"> - guide.html, terms.html):
 *
 * 1. Side contents: on a wide window the list moves to a column beside the
 *    text and stays in view while scrolling (CSS position: sticky). The
 *    section being read is highlighted, and the list scrolls itself to keep
 *    that entry visible. On a narrow window the list stays where it is in
 *    the page, above the first section.
 * 2. Clicking an entry scrolls smoothly to its section (instantly for
 *    people who asked their system for reduced motion) and updates the
 *    address, so the link can be shared.
 * 3. "Top" and "Bottom" buttons in the corner jump to either end of the
 *    page; each appears only when there is somewhere to go.
 *
 * Without JavaScript the page still works: the list is a plain set of
 * links in the text.
 */

const WIDE = window.matchMedia("(min-width: 1100px)");
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

function scrollBehavior() {
  return REDUCED_MOTION.matches ? "auto" : "smooth";
}

function mountSideContents(toc) {
  const main = toc.closest("main");
  const layout = document.createElement("div");
  layout.className = "doc-layout";
  const aside = document.createElement("aside");
  aside.className = "doc-side";
  const homeMarker = document.createComment("doc-toc");        // where the list goes back to on a narrow window
  toc.before(homeMarker);
  main.before(layout);
  layout.append(aside, main);

  const place = () => {
    if (WIDE.matches) aside.append(toc);
    else homeMarker.after(toc);
    layout.classList.toggle("has-side", WIDE.matches);
  };
  place();
  WIDE.addEventListener("change", place);
}

function highlightCurrentSection(toc) {
  const links = new Map();
  for (const link of toc.querySelectorAll('a[href^="#"]')) {
    const section = document.getElementById(decodeURIComponent(link.hash.slice(1)));
    if (section) links.set(section, link);
  }
  if (!links.size) return;
  const sections = [...links.keys()];

  let current = null;
  const update = () => {
    // The last section whose heading has passed the top quarter of the window.
    const line = window.innerHeight * 0.25;
    let found = sections[0];
    for (const section of sections) {
      if (section.getBoundingClientRect().top <= line) found = section;
    }
    // At the very bottom, the last section counts as read even if its heading never got that high.
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) found = sections.at(-1);
    if (found === current) return;
    if (current) links.get(current).removeAttribute("aria-current");
    current = found;
    const link = links.get(current);
    link.setAttribute("aria-current", "location");
    if (WIDE.matches) keepVisible(toc, link);
  };

  let pending = false;
  window.addEventListener("scroll", () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; update(); });
  }, { passive: true });
  window.addEventListener("resize", update);
  update();

  toc.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    const section = document.getElementById(decodeURIComponent(link.hash.slice(1)));
    if (!section) return;
    event.preventDefault();
    section.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
    history.replaceState(null, "", link.hash);
  });
}

/** Scroll the side list (only the list - never the page) so `link` is inside it. */
function keepVisible(list, link) {
  const box = list.getBoundingClientRect();
  const item = link.getBoundingClientRect();
  if (item.top < box.top) list.scrollTop -= box.top - item.top + 8;
  else if (item.bottom > box.bottom) list.scrollTop += item.bottom - box.bottom + 8;
}

function mountJumpButtons() {
  const box = document.createElement("div");
  box.className = "jump-buttons";
  box.innerHTML = `
    <button type="button" class="jump-btn" data-jump="top" aria-label="Back to the top of the page">↑ Top</button>
    <button type="button" class="jump-btn" data-jump="bottom" aria-label="Go to the bottom of the page">↓ Bottom</button>`;
  document.body.append(box);
  const top = box.querySelector('[data-jump="top"]');
  const bottom = box.querySelector('[data-jump="bottom"]');
  top.addEventListener("click", () => window.scrollTo({ top: 0, behavior: scrollBehavior() }));
  bottom.addEventListener("click", () =>
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: scrollBehavior() }));

  const update = () => {
    const y = window.scrollY;
    const end = document.documentElement.scrollHeight - window.innerHeight;
    top.hidden = y < 300;
    bottom.hidden = end - y < 300;
  };
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  update();
}

const toc = document.querySelector("main .doc-toc");
if (toc) {
  mountSideContents(toc);
  highlightCurrentSection(toc);
  mountJumpButtons();
}

/**
 * How people are shown: display names (backend/app/display_names.py) in
 * place of usernames. GET /users/directory gives every person's name and
 * its parts once per page (loadPeople(), called by js/topbar.js); then
 *
 *   personHtml(username)  the name as HTML - the secondary part (the location)
 *                         in a lighter <span class="person-secondary">
 *   personText(username)  the name as plain text (toasts, titles, menus)
 *
 * An unknown username (e.g. created after the page loaded) is shown as it is.
 */
import { api } from "./api.js";

let people = {};

export async function loadPeople() {
  try {
    people = (await api.peopleDirectory()).people || {};
  } catch {
    people = {};                                 // usernames are shown instead
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function personText(username) {
  if (!username) return "";
  return people[username] ? people[username].display_name : username;
}

/** Parts as HTML: [[text, secondary], ...] (also used for a preview from the server). */
export function partsHtml(parts) {
  return parts.map(([text, secondary]) =>
    secondary ? `<span class="person-secondary">${escapeHtml(text)}</span>` : escapeHtml(text)).join("");
}

export function personHtml(username) {
  if (!username) return "";
  const person = people[username];
  const html = person ? partsHtml(person.display_parts) : escapeHtml(username);
  return `<span class="person" title="${escapeHtml(username)}">${html}</span>`;
}

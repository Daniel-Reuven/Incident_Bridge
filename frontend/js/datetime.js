/**
 * How the app shows a moment in time: how long ago, then the exact time in
 * UTC, e.g.
 *
 *     formatWhen("2026-06-01T16:11:00Z")  ->  "13 days ago, 16:11 UTC 2026/06/01"
 *
 * UTC (not the viewer's local time) so everyone on the team reads the same
 * clock when they compare notes. timeAgo() alone is used where space is
 * short; formatUtc() alone where "ago" adds nothing.
 */

export function timeAgo(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return plural(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return plural(hours, "hour");
  return plural(Math.round(hours / 24), "day");
}

export function formatUtc(iso) {
  const d = new Date(iso);
  const two = (n) => String(n).padStart(2, "0");
  return `${two(d.getUTCHours())}:${two(d.getUTCMinutes())} UTC `
    + `${d.getUTCFullYear()}/${two(d.getUTCMonth() + 1)}/${two(d.getUTCDate())}`;
}

export function formatWhen(iso) {
  return iso ? `${timeAgo(iso)}, ${formatUtc(iso)}` : "";
}

function plural(n, unit) {
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

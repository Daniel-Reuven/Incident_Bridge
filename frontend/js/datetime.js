/**
 * How the app shows a moment in time: how long ago, then the exact time in
 * the VIEWER'S OWN time zone (the browser's), with the zone's name, e.g.
 *
 *     formatWhen("2026-06-01T16:11:00Z")  ->  "13 days ago, 19:11 GMT+3 2026/06/01"   (in Israel)
 *                                         ->  "13 days ago, 12:11 EDT 2026/06/01"     (in New York)
 *
 * The server stores and sends every time in UTC (ISO 8601); only the display
 * is converted, so two people in different time zones see the same moment in
 * their own clock, each labeled with its zone. timeAgo() alone is used where
 * space is short; formatLocal() alone where "ago" adds nothing.
 */

export function timeAgo(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return plural(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (hours < 24) return plural(hours, "hour");
  return plural(Math.round(hours / 24), "day");
}

export function formatLocal(iso) {
  const d = new Date(iso);
  const two = (n) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())} ${zoneName(d)} `
    + `${d.getFullYear()}/${two(d.getMonth() + 1)}/${two(d.getDate())}`;
}

export function formatWhen(iso) {
  return iso ? `${timeAgo(iso)}, ${formatLocal(iso)}` : "";
}

/** The browser's short name for its time zone at that moment ("GMT+3", "EDT", "UTC"...). */
function zoneName(date) {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
      .formatToParts(date).find((p) => p.type === "timeZoneName");
    if (part) return part.value;
  } catch {
    // very old browser: fall through to the numeric offset
  }
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return `UTC${sign}${Math.floor(abs / 60)}${abs % 60 ? `:${String(abs % 60).padStart(2, "0")}` : ""}`;
}

function plural(n, unit) {
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

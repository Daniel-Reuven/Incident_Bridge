"""
Reading the activity log back (app/audit.py writes it) - for the admin's
Logs page (app/api/logs.py).

A month is read from wherever it is: the file of the current month
(incident_bridge-YYYY-MM.jsonl, one encrypted line per record) or the
archive of a finished one (incident_bridge-YYYY-MM.zip.enc - an encrypted
zip, every member in it), or both if a month was archived while the server
still had a few lines to add. Everything is decrypted with the
installation's LogCipher (app/log_crypto.py); a line or archive that does
not decrypt counts as unreadable.

query() filters ONE month and returns the newest records first, a page at a
time, plus the "facets" of that month - every category, event, user and
level that occurs in it, with counts - which the page uses to fill its
filter menus. A line that is not valid JSON is skipped and counted.
"""

import io
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from app.audit import FILE_PREFIX, MONTH_ARCHIVE, MONTH_FILE
from app.log_crypto import LogCipher, LogDecryptError

LEVELS = ("INFO", "WARNING", "ERROR")


def available_months(log_dir: Path, archive_dir: Path) -> List[Dict[str, object]]:
    """Every month that has records, newest first: [{"month", "archived", "bytes"}]."""
    months: Dict[str, Dict[str, object]] = {}
    for folder, pattern, archived in ((Path(log_dir), MONTH_FILE, False), (Path(archive_dir), MONTH_ARCHIVE, True)):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            match = pattern.match(path.name)
            if match:
                entry = months.setdefault(match.group(1), {"month": match.group(1), "archived": False, "bytes": 0})
                entry["archived"] = entry["archived"] or archived
                entry["bytes"] += path.stat().st_size
    return [months[m] for m in sorted(months, reverse=True)]


def _lines(log_dir: Path, archive_dir: Path, month: str, cipher: Optional[LogCipher] = None) -> Iterator[Optional[str]]:
    """
    The month's lines, oldest first, as plain JSON text - decrypted with
    `cipher` (app/log_crypto.py) - or None for a line or archive that does
    not decrypt (changed, or not from this installation).
    """
    encrypted = Path(archive_dir) / f"{FILE_PREFIX}-{month}.zip.enc"
    plain_zip = Path(archive_dir) / f"{FILE_PREFIX}-{month}.zip"
    for archive in (plain_zip, encrypted):
        if not archive.is_file():
            continue
        data = archive.read_bytes()
        if archive is encrypted:
            if cipher is None:
                yield None
                continue
            try:
                data = cipher.decrypt_archive(data, month)
            except LogDecryptError:
                yield None
                continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zipped:
                for member in sorted(zipped.namelist()):
                    for raw in zipped.read(member).decode("utf-8", errors="replace").splitlines():
                        yield raw
        except zipfile.BadZipFile:
            yield None
    current = Path(log_dir) / f"{FILE_PREFIX}-{month}.jsonl"
    if current.is_file():
        with open(current, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if cipher is None or not raw.strip():
                    yield raw
                    continue
                try:
                    yield cipher.decrypt_line(raw.strip(), month)
                except LogDecryptError:
                    yield None


def query(log_dir: Path, archive_dir: Path, month: str, *, category: Optional[str] = None,
          event: Optional[str] = None, user: Optional[str] = None, level: Optional[str] = None,
          text: Optional[str] = None, limit: int = 200, offset: int = 0,
          cipher: Optional[LogCipher] = None) -> dict:
    """
    One month's records matching every given filter, newest first:
        category / event   exact match ("incident", "incident.claimed");
                           "!" EXCLUDES categories ("!http,data": all but HTTP requests and data records)
        user               exact match, ignoring letter case
        level              this level AND MORE SEVERE ("WARNING" = warnings and errors)
        text               anywhere in the record, ignoring letter case
    Returns {"month", "entries" (one page), "total" (all matches), "offset",
             "limit", "facets", "unreadable" (lines that were not JSON)}.
    """
    min_level = LEVELS.index(level) if level in LEVELS else 0
    needle = text.lower() if text else None
    wanted_user = user.lower() if user else None
    matches: List[tuple] = []          # (ts, line number, record) - the line number orders same-ms records
    facets = {"category": Counter(), "event": Counter(), "user": Counter(), "level": Counter()}
    unreadable = 0
    excluded = set(category[1:].split(",")) if category and category.startswith("!") else set()
    for number, line in enumerate(_lines(log_dir, archive_dir, month, cipher)):
        if line is None:
            unreadable += 1
            continue
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(record, dict):
            unreadable += 1
            continue
        for key in facets:
            if record.get(key):
                facets[key][record[key]] += 1
        if excluded:
            if record.get("category") in excluded:
                continue
        elif category and record.get("category") != category:
            continue
        if event and record.get("event") != event:
            continue
        if wanted_user and str(record.get("user") or "").lower() != wanted_user:
            continue
        if min_level and (record.get("level") not in LEVELS or LEVELS.index(record["level"]) < min_level):
            continue
        if needle and needle not in line.lower():
            continue
        matches.append((str(record.get("ts") or ""), number, record))
    matches.sort(key=lambda match: match[:2], reverse=True)
    return {
        "month": month, "total": len(matches), "offset": offset, "limit": limit,
        "entries": [record for _, _, record in matches[offset:offset + limit]],
        "facets": {key: dict(sorted(counter.items())) for key, counter in facets.items()},
        "unreadable": unreadable,
    }


def iter_all_months(log_dir: Path, archive_dir: Path, cipher: Optional[LogCipher] = None):
    """
    Every month, oldest first, as (month, records) - records is a generator
    of parsed dicts, or None for a line that is not a JSON object. For the
    integrity check (app/audit_integrity.py's verify()), which needs the
    lines in the order they were written.
    """
    def parsed(month):
        for line in _lines(log_dir, archive_dir, month, cipher):
            if line is None:
                yield None
                continue
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                yield None
                continue
            yield record if isinstance(record, dict) else None

    for entry in reversed(available_months(log_dir, archive_dir)):
        yield entry["month"], parsed(entry["month"])

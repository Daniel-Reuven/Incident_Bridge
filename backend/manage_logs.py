"""
The activity log from the command line. The log files are encrypted
(app/log_crypto.py), so tools like jq or a spreadsheet need a readable copy:

    python manage_logs.py export                      every month -> incident_bridge-log-export.jsonl
    python manage_logs.py export --month 2026-10      one month
    python manage_logs.py export --out C:\\safe\\log.jsonl

Run from backend/ (the server may be running). The copy is NOT encrypted -
keep it as carefully as the database itself, and delete it when done.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


def main(argv=None) -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    from app.audit import installation_log_dirs
    from app.log_crypto import LogCipher
    from app.log_reader import iter_all_months
    from app.security.installation import Installation, InstallationError
    from app.settings_store import SqliteSettingsStore

    parser = argparse.ArgumentParser(description="Export Incident Bridge's activity log as readable JSON Lines.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write a readable copy of the log")
    export.add_argument("--month", help="only this month (YYYY-MM)")
    export.add_argument("--out", default="incident_bridge-log-export.jsonl", help="the file to write")
    args = parser.parse_args(argv)

    installation = Installation.from_env()
    try:
        installation.unlock()
    except InstallationError as exc:
        sys.exit(str(exc))
    settings = SqliteSettingsStore(installation.db_path)
    log_id = (settings.get("installation") or {}).get("id")
    settings.close()
    if not log_id:
        sys.exit("This installation has no activity log yet.")
    import json
    written = unreadable = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for month, records in iter_all_months(*installation_log_dirs(log_id), LogCipher.for_database()):
            if args.month and month != args.month:
                continue
            for record in records:
                if record is None:
                    unreadable += 1
                    continue
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
    print(f"Wrote {written} record(s) to {args.out}" + (f"; {unreadable} line(s) did not decrypt" if unreadable else "")
          + ". The copy is NOT encrypted - delete it when done.")


if __name__ == "__main__":
    main()

"""
Move the installation's data to another folder - with the server STOPPED.

    python move_data.py D:\\IncidentBridge\\data
    python move_data.py /srv/incident-bridge/data --logs /var/log/incident-bridge

What it does:
  1. refuses while the server is running (it keeps the database and the
     current log file open);
  2. checks the new folder(s) are empty or do not exist yet;
  3. copies the database and its keys file into the new data folder, and the
     activity log - when it lives inside the data folder (the default), or to
     --logs when given; the folder markers (.incident-bridge) go along;
  4. checks the copy: the database opens with the key and is the same
     installation, and every log line decrypts;
  5. prints the lines to put in backend/.env.
The old folder is left as it is: start the server from the new one, check
it, then delete the old folder yourself. The machine key (credential store,
or the key file in your app-data folder) does not move and keeps working.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv


def _fail(message: str) -> None:
    sys.exit(f"Not moved: {message}")


def main(argv=None) -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    from app import database
    from app.audit import log_dirs_from_env
    from app.log_crypto import LogCipher
    from app.log_reader import iter_all_months
    from app.security.data_folders import check_new, running_pid
    from app.security.installation import Installation, InstallationError
    from app.settings_store import SqliteSettingsStore

    parser = argparse.ArgumentParser(description="Move Incident Bridge's data folder (server stopped).")
    parser.add_argument("target", help="the new data folder (empty, or not existing yet)")
    parser.add_argument("--logs", help="a separate new folder for the activity log")
    args = parser.parse_args(argv)

    installation = Installation.from_env()
    if installation.status() != "installed":
        _fail("there is no installed data at the current paths (check backend/.env).")
    db_file = Path(installation.db_path).resolve()
    old_data = db_file.parent
    pid = running_pid(old_data)
    if pid:
        _fail(f"the server is running (process {pid}) - stop it first.")
    try:
        installation.unlock()
    except InstallationError as exc:
        _fail(str(exc))
    settings = SqliteSettingsStore(str(db_file))
    installation_id = (settings.get("installation") or {}).get("id")
    settings.close()

    target = Path(args.target).expanduser().resolve()
    log_root, _archive, logs_enabled = log_dirs_from_env()
    log_root = log_root.resolve()
    logs_inside = logs_enabled and (log_root == old_data or old_data in log_root.parents)
    new_logs = Path(args.logs).expanduser().resolve() if args.logs else (
        target / log_root.relative_to(old_data) if logs_inside else None)
    wanted = [("data", target)] + ([("activity log", new_logs)] if new_logs else [])
    crowded = [f for f in check_new(wanted) if not f["ok"]]
    if crowded:
        _fail("; ".join(f"{f['path']} is {f['problem']}" for f in crowded))
    if target == old_data:
        _fail("that is the current folder.")

    keys_file = installation.keys_path.resolve()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_file, target / db_file.name)
    if keys_file.parent == old_data:
        shutil.copy2(keys_file, target / keys_file.name)
    marker = old_data / ".incident-bridge"
    if marker.exists():
        shutil.copy2(marker, target / marker.name)
    if new_logs and logs_enabled and log_root.exists():
        shutil.copytree(log_root, new_logs, dirs_exist_ok=True)

    # Check the copy before anyone relies on it.
    conn = database.connect(str(target / db_file.name))
    conn.close()
    copied = SqliteSettingsStore(str(target / db_file.name))
    same = (copied.get("installation") or {}).get("id") == installation_id
    copied.close()
    if not same:
        _fail(f"the copy in {target} does not read back as the same installation - nothing in .env changes; "
              f"delete {target} and try again.")
    unreadable = 0
    if new_logs and installation_id:
        own = new_logs / installation_id
        archive = own / "archive"
        for _month, records in iter_all_months(own, archive, LogCipher.for_database()):
            unreadable += sum(1 for record in records if record is None)
    if unreadable:
        _fail(f"{unreadable} log line(s) in the copy do not decrypt - delete {target} and try again.")

    print(f"Copied to {target}" + (f" (activity log: {new_logs})" if new_logs else "") + " and checked.\n")
    print("Now put this in backend/.env (and remove DATABASE_PATH / DATABASE_KEYS_PATH if you had set them):\n")
    print(f"    DATA_DIR={target}")
    if args.logs or (logs_enabled and not logs_inside):
        print(f"    LOG_DIR={new_logs or log_root}")
    if keys_file.parent != old_data:
        print(f"    DATABASE_KEYS_PATH={keys_file}          (the keys file stays where it is)")
    print(f"\nStart the server; when everything looks right, delete the old folder: {old_data}")


if __name__ == "__main__":
    main()

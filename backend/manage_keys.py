"""
The installation's keys from the command line (app/security/installation.py).
Run from backend/, with the server STOPPED:

    python manage_keys.py status             where the keys are, the key fingerprint
    python manage_keys.py restore            open the database with the RECOVERY KEY and give this
                                             machine (or user account) a new machine key - after
                                             moving to another computer, or when the key store or
                                             the keys file was lost
    python manage_keys.py new-recovery-key   a new recovery key, and the whole database and activity log
                                             re-encrypted with the new keys (for a recovery key that
                                             may have leaked); the old recovery key stops working

The database is DATABASE_PATH from backend/.env (default backend/incident_bridge.db);
KEY_PROVIDER / MACHINE_KEY_DIR choose where restore puts the new machine key.
"""

import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv


def _ask(prompt: str) -> str:
    """Hidden typing in a real terminal; visible (with a warning) in an IDE console that cannot hide it."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("This console cannot hide what you type - it WILL BE VISIBLE.", flush=True)
        print(prompt, end="", flush=True)
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(prompt)


def cmd_status(installation, _args) -> None:
    for name, value in installation.describe().items():
        print(f"{name.replace('_', ' '):>12}: {value if value is not None else '-'}")
    warning = installation.machine_warning() if installation.status() == "installed" else None
    if warning:
        print(f"\nWarning: {warning}")


def cmd_restore(installation, _args) -> None:
    from app.security.installation import InstallationError
    from app.security.crypto import WrongKey
    text = _ask("Recovery key (13 groups of 4, dashes optional): ")
    try:
        provider = installation.restore(text)
    except (InstallationError, WrongKey) as exc:
        sys.exit(f"Not restored: {exc}")
    print(f"Restored. The machine key is now in {installation.describe()['machine_key']}.")
    warning = installation.machine_warning()
    if warning:
        print(f"Warning: {warning}")
    print(f"Start the server as usual (key store: {provider.name}).")


def cmd_new_recovery_key(installation, _args) -> None:
    from app import database
    from app.security import crypto
    from app.security.installation import InstallationError
    from app.security.key_providers import provider_named
    try:
        installation.unlock()
    except InstallationError as exc:
        sys.exit(str(exc))
    recovery = crypto.new_recovery_key()
    new_key = crypto.database_key(crypto.parse_recovery_key(recovery))
    print("\nYOUR NEW RECOVERY KEY - store it now, offline (password manager, printout in a safe):\n")
    print(f"    {recovery}\n")
    typed = _ask("Type its last two groups to confirm you saved it (anything else cancels): ")
    if "".join(typed.upper().split()).replace("-", "") != "".join(recovery.split("-")[-2:]):
        sys.exit("Cancelled - nothing changed; the old recovery key still works.")
    from app.audit import installation_log_dirs, reencrypt_logs
    from app.log_crypto import LogCipher
    from app.settings_store import SqliteSettingsStore
    old_cipher = LogCipher.for_database()
    settings = SqliteSettingsStore(installation.db_path)
    log_id = (settings.get("installation") or {}).get("id")
    settings.close()
    conn = database.connect(installation.db_path)
    try:
        conn.execute(f"PRAGMA rekey = \"x'{new_key.hex()}'\"")       # re-encrypts every page of the file
    finally:
        conn.close()
    database.use_key(new_key)
    if log_id:                                                       # the activity log's key derives from it too
        count = reencrypt_logs(*installation_log_dirs(log_id), old_cipher, LogCipher.for_database())
        print(f"Activity log re-encrypted ({count} file(s)).")
    try:
        same_store = provider_named(installation.read()["machine"]["provider"])
        installation.restore(recovery, same_store, new_key=True)     # a new machine slot for the new key
    except InstallationError as exc:
        sys.exit(f"The database was re-encrypted, but the keys file could not be updated ({exc}). "
                 f"Run: python manage_keys.py restore - with the NEW recovery key.")
    print("Done: the database is re-encrypted; only the new recovery key works from now on.")


def main(argv=None) -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    from app.security.installation import Installation        # after .env: DATABASE_PATH, KEY_PROVIDER

    parser = argparse.ArgumentParser(description="Manage Incident Bridge's encryption keys (server stopped).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="where the keys are")
    commands.add_parser("restore", help="new machine key from the recovery key")
    commands.add_parser("new-recovery-key", help="new recovery key; the database is re-encrypted")
    args = parser.parse_args(argv)
    installation = Installation.from_env()
    {"status": cmd_status, "restore": cmd_restore, "new-recovery-key": cmd_new_recovery_key}[args.command](
        installation, args)


if __name__ == "__main__":
    main()

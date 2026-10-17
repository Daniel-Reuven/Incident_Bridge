"""
User accounts from the command line - for when nobody can log in as an
admin any more (forgotten password, lockout, a disabled account). Normal
user management is the admin's Users page in the app.

Run from backend/, with the server STOPPED (the running server keeps its
own copy of the users in memory and would not see the change):

    python manage_users.py list
    python manage_users.py reset-password admin          # asks for the new password twice
    python manage_users.py reset-password admin --no-change-required
    python manage_users.py enable tech2

reset-password follows the normal password policy, ends the user's
sessions and - unless --no-change-required - makes them choose a new
password at their next login. It uses the same database as the app
(DATABASE_PATH from backend/.env, default backend/incident_bridge.db).
"""

import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.models.system_users import is_reserved_username  # noqa: E402 - after load_dotenv
from app.persistence_users import SqliteUserStore  # noqa: E402
from app.state import database_path  # noqa: E402


def _find(store: SqliteUserStore, username: str):
    user = next((u for u in store.load_all() if u.username == username), None)
    if user is None or is_reserved_username(username):
        sys.exit(f"No user named {username!r} in {store.db_path}.")
    return user


def cmd_list(store: SqliteUserStore, _args) -> None:
    users = store.load_all()
    if not users:
        print(f"No users yet in {store.db_path} - they are created from INCIDENT_BRIDGE_USERS at the first start.")
        return
    for user in users:
        flags = [flag for flag, on in (("disabled", user.disabled), ("must change password", user.must_change_password))
                 if on]
        print(f"{user.username:<20} {user.role.value:<6} {', '.join(flags)}")


def cmd_reset_password(store: SqliteUserStore, args) -> None:
    user = _find(store, args.username)
    password = getpass.getpass(f"New password for {user.username}: ")
    if password != getpass.getpass("Repeat the new password: "):
        sys.exit("The passwords do not match - nothing changed.")
    try:
        user.reset_password(password)
    except ValueError as exc:
        sys.exit(f"Not accepted: {exc}")
    if args.no_change_required:
        user.must_change_password = False
    store.save(user)
    print(f"Password of {user.username} reset"
          f"{'' if args.no_change_required else ' - they must change it at their next login'}.")


def cmd_enable(store: SqliteUserStore, args) -> None:
    user = _find(store, args.username)
    user.set_disabled(False)
    store.save(user)
    print(f"{user.username} is enabled.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Manage Incident Bridge user accounts (server stopped).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list every account")
    reset = commands.add_parser("reset-password", help="set a new password for an account")
    reset.add_argument("username")
    reset.add_argument("--no-change-required", action="store_true",
                       help="do not force a password change at the next login")
    enable = commands.add_parser("enable", help="enable a disabled account")
    enable.add_argument("username")
    args = parser.parse_args(argv)

    store = SqliteUserStore(database_path())
    try:
        {"list": cmd_list, "reset-password": cmd_reset_password, "enable": cmd_enable}[args.command](store, args)
    finally:
        store.close()


if __name__ == "__main__":
    main()

"""
The installation's folders, kept to the app alone.

    DATA_DIR       default backend/instance/ (git-ignored) - the database, its keys
                   file and the log folder live here unless set one by one
                   (DATABASE_PATH, DATABASE_KEYS_PATH, LOG_DIR, LOG_ARCHIVE_DIR)

New installation (the setup page): every folder the installation will use
must be EMPTY or not exist yet - a folder holding anything else (another
app's files, an earlier installation's log) is refused, with what is in it.
Installed: each folder carries a marker, .incident-bridge, naming the
installation; at every start the markers must be there and name this
installation, so a path pointed at the wrong folder stops the app instead
of mixing data. Moving the data: move_data.py (server stopped).

The machine key file (KEY_PROVIDER=file) is deliberately NOT here - it lives
in the user's own app-data folder (app/security/key_providers.py), so a
copy of the data folder does not carry the key that opens it.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = BACKEND_DIR / "instance"
MARKER = ".incident-bridge"
LOCK = ".incident-bridge.lock"


def resolve(raw: Optional[str], default: Path) -> Path:
    """A path setting: unset -> `default`; relative -> against backend/."""
    if not raw or not raw.strip():
        return default
    path = Path(raw.strip()).expanduser()
    return path if path.is_absolute() else (BACKEND_DIR / path)


def data_dir() -> Path:
    return resolve(os.environ.get("DATA_DIR"), DEFAULT_DATA_DIR)


def installation_folders(db_path: str, keys_path: Optional[Path], log_root: Path,
                         archive_root: Optional[Path]) -> List[Tuple[str, Path]]:
    """[(role, folder)] - each folder once, its roles joined ("database, keys")."""
    roles: Dict[Path, List[str]] = {}
    entries = [("database", Path(db_path).resolve().parent)]
    if keys_path is not None:
        entries.append(("keys file", Path(keys_path).resolve().parent))
    entries.append(("activity log", Path(log_root).resolve()))
    if archive_root is not None:
        entries.append(("log archive", Path(archive_root).resolve()))
    for role, folder in entries:
        roles.setdefault(folder, []).append(role)
    return [(", ".join(names), folder) for folder, names in roles.items()]


def _clean(folder: Path, ours: set) -> Tuple[bool, List[str]]:
    """(clean, what else is there): empty, or holding only other folders of this installation that are clean."""
    if not folder.exists():
        return True, []
    if not folder.is_dir():
        return False, [f"{folder.name} is a file, not a folder"]
    others = []
    for entry in sorted(folder.iterdir()):
        if entry.resolve() in ours and entry.is_dir():
            clean, inside = _clean(entry.resolve(), ours)
            others += [f"{entry.name}/{name}" for name in inside]
        else:
            others.append(entry.name + ("/" if entry.is_dir() else ""))
    return not others, others


def check_new(folders: List[Tuple[str, Path]]) -> List[dict]:
    """For the setup page: every folder with "ok" and, if not empty, what is in it (the first 10 names)."""
    ours = {folder for _, folder in folders}
    report = []
    for role, folder in folders:
        clean, others = _clean(folder, ours)
        report.append({"role": role, "path": str(folder), "ok": clean,
                       "problem": None if clean else f"not empty: {len(others)} item(s)",
                       "contents": others[:10]})
    return report


def write_markers(folders: List[Tuple[str, Path]], installation_id: str) -> None:
    for role, folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({
            "app": "Incident Bridge", "installation_id": installation_id, "role": role,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "This folder belongs to one Incident Bridge installation. Keep it to the app alone; "
                    "move it with move_data.py."}, indent=2) + "\n", encoding="utf-8")


def check_markers(folders: List[Tuple[str, Path]], installation_id: str) -> List[str]:
    """Problems (empty list = fine): a folder without this installation's marker."""
    problems = []
    for role, folder in folders:
        try:
            marker = json.loads((folder / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{folder} ({role}) is not marked as this installation's folder - is the path in .env "
                            f"right? Move data with move_data.py, which keeps the markers.")
            continue
        if marker.get("installation_id") != installation_id:
            problems.append(f"{folder} ({role}) belongs to another installation ({marker.get('installation_id')}).")
    return problems


# --- "the server is running" - so move_data.py never copies files in use -----------------------

def write_lock(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LOCK).write_text(json.dumps({"pid": os.getpid(),
                                           "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}))


def remove_lock(folder: Path) -> None:
    try:
        (folder / LOCK).unlink()
    except OSError:
        pass


def running_pid(folder: Path) -> Optional[int]:
    """The server's process id when it is running on this folder, else None (a stale lock is ignored)."""
    try:
        pid = int(json.loads((folder / LOCK).read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return pid if _alive(pid) else None


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

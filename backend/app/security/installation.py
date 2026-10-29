"""
The installation's keys: how the encrypted database (app/database.py) is
opened at every start, created at setup, and recovered.

The keys, like a disk-encryption tool's:

    recovery key     256 random bits, shown ONCE on the setup page for the admin
                     to keep offline (a password manager, a printout in a safe).
    database key     derived from the recovery key (crypto.database_key) - it
                     encrypts the database (and, later, the log files).
    machine key      a random key-encryption key kept by the operating system
                     (app/security/key_providers.py); it unlocks a wrapped copy of
                     the database key, so the app starts by itself on this machine.

The keys file, next to the database (<DATABASE_PATH>.keys, or
DATABASE_KEYS_PATH), holds nothing secret on its own:

    {"format": 1, "install_id": "...", "created_at": "...",
     "fingerprint": "8 hex digits of the database key",
     "machine": {"provider": "keyring", "info": {...}, "wrapped": {"nonce", "ciphertext"}}}

Status at startup (status()):
    "new"        no keys file and no database: the setup page runs
    "installed"  keys file present: unlock() opens the database by itself
    "orphan"     a database without its keys file: refuse to start - restore
                 with the recovery key (manage_keys.py restore), or remove the
                 database for a new installation

A database at ":memory:" (tests) gets an in-memory installation: its keys
live only as long as the process.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from app import database
from app.security import crypto
from app.security.key_providers import KeyProvider, KeyProviderError, MemoryProvider, choose_provider, provider_named

FORMAT = 1


class InstallationError(RuntimeError):
    """The installation cannot be opened - the message says what to do."""


class Installation:
    def __init__(self, db_path: str, keys_path: Optional[Path]):
        self.db_path = db_path
        self.keys_path = keys_path                     # None: an in-memory installation
        self._memory_file: Optional[dict] = None
        self._memory_provider = MemoryProvider()

    @classmethod
    def from_env(cls) -> "Installation":
        from app.state import database_path          # here: app.state imports much of the app
        db_path = database_path()
        if db_path == ":memory:":
            return cls(db_path, None)
        configured = (os.environ.get("DATABASE_KEYS_PATH") or "").strip()
        return cls(db_path, Path(configured) if configured else Path(db_path + ".keys"))

    def folders(self):
        """[(role, folder)] this installation keeps to itself (app/security/data_folders.py); none in memory."""
        if self.keys_path is None:
            return []
        from app.audit import log_dirs_from_env
        from app.security.data_folders import installation_folders
        log_root, archive_root, enabled = log_dirs_from_env()
        if not enabled:
            log_root = None
        separate_archive = archive_root if enabled and (os.environ.get("LOG_ARCHIVE_DIR") or "").strip() else None
        folders = installation_folders(self.db_path, self.keys_path, log_root or Path(self.db_path).parent,
                                       separate_archive)
        return folders

    # --- state ------------------------------------------------------------------

    def read(self) -> Optional[dict]:
        if self.keys_path is None:
            return self._memory_file
        try:
            return json.loads(self.keys_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise InstallationError(f"The keys file {self.keys_path} cannot be read ({exc}).") from None

    def status(self) -> str:
        if self.read() is not None:
            return "installed"
        if self.keys_path is not None and Path(self.db_path).exists():
            return "orphan"
        return "new"

    def _write(self, data: dict) -> None:
        if self.keys_path is None:
            self._memory_file = data
            return
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.keys_path.with_name(self.keys_path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.keys_path)        # never a half-written keys file

    def _provider(self, data: dict) -> KeyProvider:
        if self.keys_path is None:
            return self._memory_provider
        try:
            return provider_named(data["machine"]["provider"])
        except KeyError:
            raise InstallationError(f"The keys file {self.keys_path} names no known key store.") from None

    # --- creating (the setup page) ------------------------------------------------

    def create(self) -> Tuple[str, bytes, dict]:
        """
        New keys for a new installation: (recovery key text, database key,
        keys-file data). Nothing is written yet - see save_new().
        """
        recovery = crypto.new_recovery_key()
        db_key = crypto.database_key(crypto.parse_recovery_key(recovery))
        data = {"format": FORMAT, "install_id": uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "fingerprint": crypto.fingerprint(db_key)}
        return recovery, db_key, data

    def save_new(self, db_key: bytes, data: dict, provider: Optional[KeyProvider] = None) -> KeyProvider:
        """Put the machine slot in the machine's store, write the keys file, unlock the database."""
        if self.status() != "new":
            raise InstallationError("This installation already has its keys.")
        provider = self._memory_provider if self.keys_path is None else (provider or choose_provider())
        data = dict(data, machine=self._machine_slot(provider, data["install_id"], db_key))
        self._write(data)
        database.use_key(db_key)
        return provider

    def _machine_slot(self, provider: KeyProvider, install_id: str, db_key: bytes) -> dict:
        machine_key = crypto.new_key()
        info = provider.store(install_id, machine_key)
        return {"provider": provider.name, "info": info,
                "wrapped": crypto.wrap(machine_key, db_key, f"machine|{install_id}")}

    # --- opening (every start) ----------------------------------------------------

    def unlock(self) -> bytes:
        """Open the database key with the machine's key and hand it to app/database.py."""
        data = self.read()
        if data is None:
            raise InstallationError("Not installed yet - finish the setup first.")
        provider = self._provider(data)
        try:
            machine_key = provider.load(data["install_id"], data["machine"].get("info", {}))
            db_key = crypto.unwrap(machine_key, data["machine"]["wrapped"], f"machine|{data['install_id']}")
        except (KeyProviderError, crypto.WrongKey, KeyError) as exc:
            raise InstallationError(f"The database cannot be unlocked on this machine: {exc} "
                                    f"Restore access with your recovery key: python manage_keys.py restore") from None
        if crypto.fingerprint(db_key) != data.get("fingerprint"):
            raise InstallationError("The keys file does not belong to this database key (fingerprint differs).")
        database.use_key(db_key)
        return db_key

    def machine_warning(self) -> Optional[str]:
        data = self.read()
        if not data:
            return None
        return self._provider(data).warning(data["install_id"], data["machine"].get("info", {}))

    def describe(self) -> dict:
        """What `manage_keys.py status` and the setup page show (nothing secret)."""
        data = self.read() or {}
        machine = data.get("machine", {})
        location = self._provider(data).describe(data["install_id"], machine.get("info", {})) if data else None
        return {"status": self.status(), "database": self.db_path,
                "keys_file": str(self.keys_path) if self.keys_path else "(in memory)",
                "install_id": data.get("install_id"), "created_at": data.get("created_at"),
                "fingerprint": data.get("fingerprint"), "key_store": machine.get("provider"),
                "machine_key": location}

    # --- recovery (manage_keys.py) --------------------------------------------------

    def restore(self, recovery_text: str, provider: Optional[KeyProvider] = None,
                new_key: bool = False) -> KeyProvider:
        """
        Rebuild the machine slot from the recovery key - on a new machine, a
        new user account, or after the keys file or the machine's store was
        lost. The database file is checked to really open with the key first.
        `new_key`: the database was just re-encrypted with a new recovery key
        (manage_keys.py new-recovery-key), so the keys file's fingerprint changes.
        """
        if self.keys_path is None:
            raise InstallationError("An in-memory installation cannot be restored.")
        db_key = crypto.database_key(crypto.parse_recovery_key(recovery_text))
        if not Path(self.db_path).exists():
            raise InstallationError(f"There is no database at {self.db_path}.")
        database.use_key(db_key)
        try:
            database.connect(self.db_path).close()
        except database.DatabaseError:
            database.forget_key()
            raise InstallationError("This recovery key does not open the database.") from None
        old = self.read() or {}
        if not new_key and old.get("fingerprint") not in (None, crypto.fingerprint(db_key)):
            raise InstallationError("This recovery key belongs to another installation than the keys file.")
        install_id = uuid.uuid4().hex                      # a fresh entry in the new machine store
        provider = provider or choose_provider()
        data = {"format": FORMAT, "install_id": install_id,
                "created_at": old.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "restored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "fingerprint": crypto.fingerprint(db_key),
                "machine": self._machine_slot(provider, install_id, db_key)}
        self._write(data)
        return provider

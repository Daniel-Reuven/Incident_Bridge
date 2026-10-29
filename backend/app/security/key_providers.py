"""
Where the machine keeps its key - the "machine slot" of the installation
(app/security/installation.py). A KeyProvider stores one secret, the
machine key-encryption key, in the operating system's protected place, so
the app unlocks the database by itself at every start on THIS machine, and
a copied database + keys file is useless anywhere else. (The recovery key,
kept by a person, is the other way in.) A Strategy: the installation does
not care which provider it gets.

    keyring   the OS credential store, through the `keyring` package:
              Windows Credential Manager (DPAPI, TPM-backed where available),
              macOS Keychain, Linux desktop Secret Service (GNOME Keyring / KWallet)
    systemd   a Linux service: systemd decrypts the credential (with the TPM2
              chip when there is one, else the root-only host key) and hands
              it to the service in $CREDENTIALS_DIRECTORY at start
    file      a key file readable only by the app's user account, outside the
              data folder - the last resort, with a warning at every start
    memory    nothing stored - for a DATABASE_PATH=":memory:" database (tests)

KEY_PROVIDER=auto (default) picks: systemd when its credential is present,
else keyring when the OS store works, else file. KEY_PROVIDER=keyring |
systemd | file forces one. MACHINE_KEY_DIR sets the file provider's folder.
"""

import base64
import os
import stat
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

SERVICE_NAME = "Incident Bridge"
CREDENTIAL_NAME = "incident-bridge-key"


class KeyProviderError(RuntimeError):
    """The provider could not store or read the machine key - the message says what to do."""


class KeyProvider(ABC):
    name = ""

    @abstractmethod
    def store(self, install_id: str, secret: bytes) -> dict:
        """Keep `secret`; returns what load() needs later (saved in the keys file - nothing secret)."""

    @abstractmethod
    def load(self, install_id: str, info: dict) -> bytes:
        """The stored secret. Raises KeyProviderError when it is not there."""

    def describe(self, install_id: str, info: dict) -> str:
        """One line for the setup page and `manage_keys.py status`: where the key is."""
        return self.name

    def warning(self, install_id: str, info: dict) -> Optional[str]:
        """A warning to show at every start, or None."""
        return None

    def preview(self) -> str:
        """Where a NEW installation's machine key will go (the setup page, before it exists)."""
        return self.describe("", {})


class KeyringProvider(KeyProvider):
    name = "keyring"

    @staticmethod
    def usable() -> bool:
        """True when `keyring` has a real, secure backend (not the 'fail', 'null' or plain-text ones)."""
        try:
            import keyring
            backend = keyring.get_keyring()
        except Exception:                                    # noqa: BLE001 - no usable store
            return False
        module = type(backend).__module__
        return backend.priority > 0 and not any(bad in module for bad in ("fail", "null", "alt", "chainer"))

    def store(self, install_id: str, secret: bytes) -> dict:
        import keyring
        try:
            keyring.set_password(SERVICE_NAME, f"database-key-{install_id}", base64.b64encode(secret).decode())
        except Exception as exc:                             # noqa: BLE001
            raise KeyProviderError(f"The system's credential store refused the key ({exc}).") from None
        return {"backend": type(keyring.get_keyring()).__module__}

    def load(self, install_id: str, info: dict) -> bytes:
        import keyring
        try:
            value = keyring.get_password(SERVICE_NAME, f"database-key-{install_id}")
        except Exception as exc:                             # noqa: BLE001
            raise KeyProviderError(f"The system's credential store could not be read ({exc}).") from None
        if not value:
            raise KeyProviderError("The machine key is not in this computer's credential store (another computer "
                                   "or user account?). Restore with: python manage_keys.py restore")
        return base64.b64decode(value)

    def describe(self, install_id: str, info: dict) -> str:
        names = {"Windows": "Windows Credential Manager", "macOS": "macOS Keychain", "SecretService":
                 "the desktop's Secret Service (GNOME Keyring / KWallet)", "kwallet": "KWallet"}
        backend = info.get("backend", "")
        where = next((label for part, label in names.items() if part in backend), backend or "the OS store")
        return f"{where}, entry \"{SERVICE_NAME}\" / database-key-{install_id}"

    def preview(self) -> str:
        import keyring
        return self.describe("<id>", {"backend": type(keyring.get_keyring()).__module__}).split(", entry")[0]


class FileProvider(KeyProvider):
    name = "file"

    def __init__(self, directory: Optional[Path] = None):
        self.directory = directory or machine_key_dir()

    def path(self, install_id: str) -> Path:
        return self.directory / f"{install_id}.key"

    def store(self, install_id: str, secret: bytes) -> dict:
        path = self.path(install_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)               # created readable by this user only
        except FileExistsError:
            raise KeyProviderError(f"{path} already exists - remove it or set another MACHINE_KEY_DIR.") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64encode(secret))
        return {"path": str(path)}

    def load(self, install_id: str, info: dict) -> bytes:
        path = Path(info.get("path") or self.path(install_id))
        try:
            return base64.b64decode(path.read_bytes())
        except OSError:
            raise KeyProviderError(f"The machine key file {path} is missing or unreadable. "
                                   f"Restore with: python manage_keys.py restore") from None

    def describe(self, install_id: str, info: dict) -> str:
        return f"the key file {info.get('path') or self.path(install_id)} (readable only by this user account)"

    def preview(self) -> str:
        return f"a key file in {self.directory} (readable only by this user account)"

    def warning(self, install_id: str, info: dict) -> Optional[str]:
        path = Path(info.get("path") or self.path(install_id))
        notes = ["the machine key is a plain file - this computer has no credential store the app can use"]
        if sys.platform != "win32" and path.exists() and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            notes.append(f"{path} can be read by other accounts - run: chmod 600 {path}")
        return "; ".join(notes) + "."


class SystemdProvider(KeyProvider):
    """
    systemd-creds. The app cannot encrypt into systemd's store itself (that
    needs root), so setup writes the machine key to a file readable only by
    this user and shows the two commands that move it into systemd; until
    then that file is used, with a warning.
    """
    name = "systemd"

    def __init__(self, directory: Optional[Path] = None):
        self.pending = FileProvider(directory)

    @staticmethod
    def credential_path() -> Optional[Path]:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        path = Path(directory) / CREDENTIAL_NAME if directory else None
        return path if path and path.exists() else None

    def store(self, install_id: str, secret: bytes) -> dict:
        return self.pending.store(install_id, secret)

    def load(self, install_id: str, info: dict) -> bytes:
        path = self.credential_path()
        if path is not None:
            return base64.b64decode(path.read_bytes())
        return self.pending.load(install_id, info)

    def describe(self, install_id: str, info: dict) -> str:
        if self.credential_path():
            return f"systemd credential {CREDENTIAL_NAME} (LoadCredentialEncrypted)"
        path = info.get("path") or self.pending.path(install_id)
        return (f"waiting to be moved into systemd: sudo systemd-creds encrypt --name={CREDENTIAL_NAME} {path} "
                f"/etc/credstore.encrypted/{CREDENTIAL_NAME} - then add LoadCredentialEncrypted={CREDENTIAL_NAME} "
                f"to the service, restart it, and delete {path}")

    def preview(self) -> str:
        return (f"systemd's encrypted credentials - the key is first written to a file in {self.pending.directory}, "
                f"with the commands that move it into systemd")

    def warning(self, install_id: str, info: dict) -> Optional[str]:
        return None if self.credential_path() else "the machine key is still a plain file: " + \
            self.describe(install_id, info)


class MemoryProvider(KeyProvider):
    """Nothing is stored: the installation lives as long as the process (a ":memory:" database)."""
    name = "memory"

    def __init__(self):
        self._secrets = {}

    def store(self, install_id: str, secret: bytes) -> dict:
        self._secrets[install_id] = secret
        return {}

    def load(self, install_id: str, info: dict) -> bytes:
        if install_id not in self._secrets:
            raise KeyProviderError("in-memory installation: the key is gone")
        return self._secrets[install_id]


def machine_key_dir() -> Path:
    """MACHINE_KEY_DIR, else the user's own application-data folder (never the project or data folder)."""
    configured = (os.environ.get("MACHINE_KEY_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "IncidentBridge" / "keys"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "IncidentBridge" / "keys"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "incident-bridge" / "keys"


PROVIDER_NAMES = ("auto", "keyring", "systemd", "file")


def provider_named(name: str) -> KeyProvider:
    """The provider a keys file names (for unlocking)."""
    return {"keyring": KeyringProvider, "systemd": SystemdProvider, "file": FileProvider}[name]()


def choose_provider() -> KeyProvider:
    """KEY_PROVIDER for a NEW installation (see the module docstring). Raises ValueError for an unknown value."""
    choice = (os.environ.get("KEY_PROVIDER") or "auto").strip().lower()
    if choice not in PROVIDER_NAMES:
        raise ValueError(f"KEY_PROVIDER must be one of: {', '.join(PROVIDER_NAMES)} (got {choice!r}).")
    if choice == "keyring" and not KeyringProvider.usable():
        raise ValueError("KEY_PROVIDER=keyring, but this computer has no usable credential store.")
    if choice != "auto":
        return provider_named(choice)
    if SystemdProvider.credential_path():
        return SystemdProvider()
    return KeyringProvider() if KeyringProvider.usable() else FileProvider()

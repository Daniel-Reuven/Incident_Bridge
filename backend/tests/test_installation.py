"""
Tests for encryption at rest and the installation's keys: app/database.py
(SQLCipher), app/security/ (crypto, key providers, installation) and
manage_keys.py. Machine keys go to a throwaway folder (KEY_PROVIDER=file,
MACHINE_KEY_DIR - tests/conftest.py), never the real credential store.

Run from backend/:  python -m pytest tests/test_installation.py -v
"""

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from app import database
from app.security import crypto
from app.security.installation import Installation, InstallationError
from app.security.key_providers import FileProvider, KeyringProvider, KeyProviderError, choose_provider


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("MACHINE_KEY_DIR", str(tmp_path / "machine-keys"))
    return Installation.from_env()


def install(installation):
    recovery, key, data = installation.create()
    installation.save_new(key, data)
    conn = database.connect(installation.db_path)
    conn.execute("CREATE TABLE t (secret TEXT)")
    conn.execute("INSERT INTO t VALUES ('Payroll system password reset')")
    conn.commit()
    conn.close()
    return recovery, key


# --- the encrypted file ---------------------------------------------------------------

def test_the_database_file_is_encrypted_and_unreadable_without_the_key(installation):
    install(installation)
    raw = Path(installation.db_path).read_bytes()
    assert b"Payroll" not in raw and b"SQLite format 3" not in raw
    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        sqlite3.connect(installation.db_path).execute("SELECT * FROM t").fetchall()
    database.use_key(crypto.new_key())                                  # a wrong key
    with pytest.raises(database.DatabaseError):
        database.connect(installation.db_path)


def test_nothing_opens_before_the_key_is_known(tmp_path):
    database.forget_key()
    with pytest.raises(database.DatabaseLocked):
        database.connect(str(tmp_path / "x.db"))


# --- keys ------------------------------------------------------------------------------

def test_the_recovery_key_format_and_the_derived_database_key():
    text = crypto.new_recovery_key()
    groups = text.split("-")
    assert len(groups) == 13 and all(len(g) == 4 for g in groups)
    loose = text.lower().replace("-", " ")                               # spaces and lower case are forgiven
    assert crypto.database_key(crypto.parse_recovery_key(loose)) == crypto.database_key(crypto.parse_recovery_key(text))
    with pytest.raises(crypto.WrongKey):
        crypto.parse_recovery_key("ABCD-EFGH")


def test_a_wrapped_key_only_opens_with_its_key_and_slot():
    kek, key = crypto.new_key(), crypto.new_key()
    wrapped = crypto.wrap(kek, key, "machine|abc")
    assert crypto.unwrap(kek, wrapped, "machine|abc") == key
    with pytest.raises(crypto.WrongKey):
        crypto.unwrap(crypto.new_key(), wrapped, "machine|abc")
    with pytest.raises(crypto.WrongKey):
        crypto.unwrap(kek, wrapped, "machine|other")                    # copied into another slot


def test_the_keys_file_holds_no_secret_and_the_machine_key_is_private(installation):
    recovery, key = install(installation)
    data = json.loads(installation.keys_path.read_text())
    text = json.dumps(data)
    assert recovery not in text and key.hex() not in text
    assert data["fingerprint"] == crypto.fingerprint(key) and data["machine"]["provider"] == "file"
    machine_file = Path(data["machine"]["info"]["path"])
    assert machine_file.parent != Path(installation.db_path).parent        # not next to the data
    if sys.platform != "win32":
        assert stat.S_IMODE(machine_file.stat().st_mode) == 0o600


# --- status, unlock, restore -----------------------------------------------------------

def test_status_new_installed_and_orphan(installation, tmp_path):
    assert installation.status() == "new"
    install(installation)
    assert installation.status() == "installed"
    installation.keys_path.unlink()
    assert installation.status() == "orphan"                             # a database without its keys


def test_every_start_unlocks_by_itself(installation):
    _, key = install(installation)
    database.forget_key()
    assert Installation.from_env().unlock() == key
    assert database.connect(installation.db_path).execute("SELECT secret FROM t").fetchone()[0].startswith("Payroll")


def test_a_lost_machine_key_is_restored_with_the_recovery_key(installation):
    recovery, key = install(installation)
    Path(json.loads(installation.keys_path.read_text())["machine"]["info"]["path"]).unlink()   # machine key lost
    database.forget_key()
    with pytest.raises(InstallationError, match="manage_keys.py restore"):
        Installation.from_env().unlock()
    installation.keys_path.unlink()                                      # and the keys file too
    Installation.from_env().restore(recovery)
    database.forget_key()
    assert Installation.from_env().unlock() == key


def test_a_wrong_recovery_key_restores_nothing(installation):
    install(installation)
    with pytest.raises(InstallationError, match="does not open the database"):
        installation.restore(crypto.new_recovery_key())


def test_an_in_memory_installation_keeps_nothing_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    installation = Installation.from_env()
    _, key, data = installation.create()
    installation.save_new(key, data)
    assert installation.status() == "installed" and installation.keys_path is None
    assert installation.unlock() == key


# --- key providers ---------------------------------------------------------------------

def test_the_file_provider_refuses_to_overwrite_and_reports_a_missing_file(tmp_path):
    provider = FileProvider(tmp_path)
    provider.store("abc", b"x" * 32)
    with pytest.raises(KeyProviderError, match="already exists"):
        provider.store("abc", b"y" * 32)
    with pytest.raises(KeyProviderError, match="manage_keys.py restore"):
        provider.load("other", {})


@pytest.mark.parametrize("value, expected", [("file", "file"), ("auto", None)])
def test_key_provider_choice(monkeypatch, value, expected):
    monkeypatch.setenv("KEY_PROVIDER", value)
    chosen = choose_provider().name
    assert chosen == (expected or ("keyring" if KeyringProvider.usable() else "file"))


def test_an_unknown_key_provider_is_refused(monkeypatch):
    monkeypatch.setenv("KEY_PROVIDER", "usb-stick")
    with pytest.raises(ValueError, match="KEY_PROVIDER must be one of"):
        choose_provider()


# --- manage_keys.py --------------------------------------------------------------------

def run_manage_keys(monkeypatch, args, *answers):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_keys
    replies = list(answers)
    monkeypatch.setattr(manage_keys, "_ask", lambda prompt: replies.pop(0))
    manage_keys.main(args)


def test_manage_keys_restore_and_status(installation, monkeypatch, capsys):
    recovery, _ = install(installation)
    installation.keys_path.unlink()
    run_manage_keys(monkeypatch, ["restore"], recovery)
    run_manage_keys(monkeypatch, ["status"])
    out = capsys.readouterr().out
    assert "Restored." in out and "status: installed" in out and crypto.fingerprint(
        crypto.database_key(crypto.parse_recovery_key(recovery))) in out


def test_manage_keys_new_recovery_key_re_encrypts_the_database(installation, monkeypatch, capsys):
    old_recovery, old_key = install(installation)
    shown = []
    real = crypto.new_recovery_key
    monkeypatch.setattr(crypto, "new_recovery_key", lambda: shown.append(real()) or shown[-1])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import manage_keys
    monkeypatch.setattr(manage_keys, "_ask", lambda prompt: "-".join(shown[-1].split("-")[-2:]))
    manage_keys.main(["new-recovery-key"])
    assert "Done: the database is re-encrypted" in capsys.readouterr().out
    database.use_key(old_key)
    with pytest.raises(database.DatabaseError):                          # the old key no longer opens it
        database.connect(installation.db_path)
    database.forget_key()
    new_key = Installation.from_env().unlock()
    assert new_key == crypto.database_key(crypto.parse_recovery_key(shown[-1]))
    assert database.connect(installation.db_path).execute("SELECT count(*) FROM t").fetchone()[0] == 1
    with pytest.raises(InstallationError):
        Installation.from_env().restore(old_recovery)


def test_manage_keys_new_recovery_key_can_be_cancelled(installation, monkeypatch, capsys):
    _, key = install(installation)
    with pytest.raises(SystemExit, match="Cancelled"):
        run_manage_keys(monkeypatch, ["new-recovery-key"], "nope")
    database.forget_key()
    assert Installation.from_env().unlock() == key                       # nothing changed
    assert os.path.exists(installation.db_path)


class _MemoryKeyring:
    """A stand-in for the OS credential store (Windows Credential Manager, Keychain, Secret Service)."""

    def __init__(self):
        import keyring.backend

        class Backend(keyring.backend.KeyringBackend):
            priority = 5
            entries = {}

            def get_password(self, service, username):
                return self.entries.get((service, username))

            def set_password(self, service, username, password):
                self.entries[(service, username)] = password

            def delete_password(self, service, username):
                self.entries.pop((service, username), None)

        self.backend = Backend()


def test_the_os_credential_store_holds_the_machine_key(installation, monkeypatch):
    import keyring
    store = _MemoryKeyring().backend
    previous = keyring.get_keyring()
    keyring.set_keyring(store)
    try:
        monkeypatch.setenv("KEY_PROVIDER", "keyring")
        recovery, key = install(installation)
        data = json.loads(installation.keys_path.read_text())
        assert data["machine"]["provider"] == "keyring"
        assert list(store.entries) == [("Incident Bridge", f"database-key-{data['install_id']}")]
        database.forget_key()
        assert Installation.from_env().unlock() == key
        store.entries.clear()                                            # e.g. another user account
        with pytest.raises(InstallationError, match="credential store"):
            Installation.from_env().unlock()
        Installation.from_env().restore(recovery)
        assert Installation.from_env().unlock() == key
    finally:
        keyring.set_keyring(previous)

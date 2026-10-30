"""
Tests for HTTPS: the server settings (app/server_settings.py - .env, the
page wins, the production rules), the certificate (app/security/tls.py -
generated, files, a chain to a certificate authority), "HTTPS only for
remote connections" with HSTS and the Secure cookie (app/api/security.py),
the Server settings page's API (app/api/server.py, also the AI settings)
and how serve_app.py plans its listeners.

Run from backend/:  python -m pytest tests/test_https.py -v
"""

import datetime as dt
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import Store
from fastapi.testclient import TestClient

from app import server_settings
from app.security import tls
from app.server_settings import Running, ServerSettings
from tests.install_helper import finish_setup

ENV = ("HOST", "PORT", "HTTPS_ENABLED", "HTTPS_PORT", "HTTPS_ONLY_REMOTE", "TLS_CERT_FILE", "TLS_KEY_FILE",
       "TLS_KEY_PASSWORD", "TRUSTED_PROXIES", "APP_MODE")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server_settings, "RUNNING", Running())
    monkeypatch.setenv(server_settings.RUNNING_ENV, "{}")       # set_running() writes it; removed again afterwards


# --- settings ---------------------------------------------------------------------------------

def test_defaults_then_env_then_the_page(monkeypatch):
    assert server_settings.effective() == ServerSettings()
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("HTTPS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5, 10.1.0.0/16")
    settings = server_settings.effective({"http_port": 8080})
    assert (settings.http_port, settings.https_enabled, settings.trusted_proxies) == (8080, True,
                                                                                       ["10.0.0.5/32", "10.1.0.0/16"])
    assert server_settings.sources({"http_port": 8080})["http_port"] == "page"
    assert server_settings.sources({})["https_enabled"] == ".env"
    monkeypatch.setenv("TLS_CERT_FILE", "/x.pem")
    assert server_settings.effective().certificate == "files"


@pytest.mark.parametrize("name, value, message", [
    ("PORT", "70000", "port number"), ("HTTPS_ENABLED", "maybe", "true or false"),
    ("TRUSTED_PROXIES", "proxy.local", "not an IP address"), ("APP_MODE", "live", "testing or production"),
])
def test_bad_values_are_refused(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        server_settings.app_mode() if name == "APP_MODE" else server_settings.effective()


def test_the_rules_and_production():
    assert server_settings.problems(ServerSettings(https_enabled=True, https_port=8000), "testing") == [
        "HTTP and HTTPS need different ports."]
    assert "needs HTTPS turned on" in server_settings.problems(ServerSettings(https_only_remote=True), "testing")[0]
    found = " ".join(server_settings.problems(ServerSettings(), "production"))
    assert "Production needs HTTPS" in found and "HTTPS only for remote" in found
    generated = ServerSettings(https_enabled=True, https_only_remote=True)
    assert "self-signed certificate is for testing only" in server_settings.problems(generated, "production")[0]
    assert server_settings.problems(ServerSettings(https_enabled=True, https_only_remote=True, certificate="files",
                                                   cert_file="a", key_file="b"), "production") == []


# --- certificates -----------------------------------------------------------------------------

def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _cert(subject, issuer, key, signer, *, ca=False, names=("app.example.com",), days=(-1, 90)):
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now + dt.timedelta(days=days[0])).not_valid_after(now + dt.timedelta(days=days[1]))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if ca:
        builder = builder.add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False),
                                        critical=True)
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    else:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                                        critical=False)
    builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
                                    critical=False)
    return builder.sign(signer, hashes.SHA256())


def _write(folder: Path, certs, key, password=None):
    cert_file, key_file = folder / "site.crt", folder / "site.key"
    cert_file.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs))
    encryption = (serialization.BestAvailableEncryption(password.encode()) if password
                  else serialization.NoEncryption())
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption))
    return str(cert_file), str(key_file)


@pytest.fixture
def authority():
    """A root CA, an intermediate and a server certificate signed by it."""
    root_key, mid_key, leaf_key = _key(), _key(), _key()
    root = _cert("Test Root", "Test Root", root_key, root_key, ca=True, days=(-10, 3650))
    mid = _cert("Test Intermediate", "Test Root", mid_key, root_key, ca=True, days=(-10, 3650))
    leaf = _cert("app.example.com", "Test Intermediate", leaf_key, mid_key)
    return {"root": root, "mid": mid, "leaf": leaf, "leaf_key": leaf_key, "mid_key": mid_key}


def test_the_generated_certificate_is_for_testing(tmp_path):
    info = tls.generate_self_signed(tmp_path, "pw")
    assert info["self_signed"] and "localhost" in info["names"] and "127.0.0.1" in info["names"]
    assert b"ENCRYPTED" in (tmp_path / tls.GENERATED_KEY).read_bytes()        # the key is never plain on disk
    testing = tls.check_files(str(tmp_path / tls.GENERATED_CERT), str(tmp_path / tls.GENERATED_KEY), "pw")
    assert testing["ok"] and "Self-signed" in testing["warnings"][0]
    production = tls.check_files(str(tmp_path / tls.GENERATED_CERT), str(tmp_path / tls.GENERATED_KEY), "pw",
                                 "production")
    assert not production["ok"] and "self-signed" in production["problems"][0]
    wrong = tls.check_files(str(tmp_path / tls.GENERATED_CERT), str(tmp_path / tls.GENERATED_KEY), "nope")
    assert "password is wrong" in wrong["problems"][0]


def test_ensure_generated_keeps_one_and_remembers_its_password(tmp_path):
    store = {}

    class Settings(dict):
        def set(self, name, value):
            self[name] = value
    store = Settings()
    first = tls.ensure_generated(str(tmp_path / "ib.db"), store)
    again = tls.ensure_generated(str(tmp_path / "ib.db"), store)
    assert first == again and Path(first[0]).parent == tmp_path / "tls" and store[tls.SETTING]["password"] == first[2]


def test_a_certificate_from_an_authority_passes_production(tmp_path, authority):
    trust = Store([authority["root"]])
    cert, key = _write(tmp_path, [authority["leaf"], authority["mid"]], authority["leaf_key"], "secret")
    result = tls.check_files(cert, key, "secret", "production", trust=trust)
    assert result["ok"], result["problems"]
    assert result["info"]["issuer"] == "Test Intermediate" and result["info"]["names"] == ["app.example.com"]


def test_production_refuses_a_broken_chain_an_unknown_authority_and_a_wrong_key(tmp_path, authority):
    trust = Store([authority["root"]])
    cert, key = _write(tmp_path, [authority["leaf"]], authority["leaf_key"])        # no intermediate
    assert "intermediate certificate" in tls.check_files(cert, key, "", "production", trust=trust)["problems"][0]
    stranger_key = _key()
    stranger = _cert("Other Root", "Other Root", stranger_key, stranger_key, ca=True)
    cert, key = _write(tmp_path, [authority["leaf"], authority["mid"]], authority["leaf_key"])
    assert not tls.check_files(cert, key, "", "production", trust=Store([stranger]))["ok"]
    cert, key = _write(tmp_path, [authority["leaf"], authority["mid"]], _key())
    assert "does not fit" in tls.check_files(cert, key, "", "testing")["problems"][0]


def test_expired_and_missing_files(tmp_path, authority):
    old = _cert("app.example.com", "Test Intermediate", authority["leaf_key"], authority["mid_key"], days=(-100, -1))
    cert, key = _write(tmp_path, [old, authority["mid"]], authority["leaf_key"])
    assert "expired" in tls.check_files(cert, key)["problems"][0]
    soon = _cert("app.example.com", "Test Intermediate", authority["leaf_key"], authority["mid_key"], days=(-1, 10))
    cert, key = _write(tmp_path, [soon, authority["mid"]], authority["leaf_key"])
    assert "renew" in tls.check_files(cert, key)["warnings"][0]
    assert "was not found" in tls.check_files(str(tmp_path / "none.crt"), key)["problems"][0]


# --- redirect, HSTS, Secure cookie --------------------------------------------------------------

def _running(monkeypatch, mode="testing", only_remote=True):
    monkeypatch.setattr(server_settings, "RUNNING", Running(http_port=8000, https_port=8443,
                                                            https_only_remote=only_remote, mode=mode))


def _client(app, base="http://ib.example.org", host="10.1.2.3"):
    return TestClient(app, base_url=base, client=(host, 5000), follow_redirects=False)


def test_remote_http_is_redirected_and_this_computer_is_not(app_client, monkeypatch):
    _running(monkeypatch)
    response = _client(app_client.app).get("/guide.html?tab=1")
    assert response.status_code == 308 and response.headers["location"] == "https://ib.example.org:8443/guide.html?tab=1"
    assert _client(app_client.app, host="127.0.0.1").get("/guide.html").status_code == 200
    assert _client(app_client.app, base="https://ib.example.org:8443").get("/guide.html").status_code == 200
    _running(monkeypatch, only_remote=False)
    assert _client(app_client.app).get("/guide.html").status_code == 200


def test_hsts_only_in_production_and_never_for_localhost(app_client, monkeypatch):
    _running(monkeypatch, mode="production")
    secure = _client(app_client.app, base="https://ib.example.org:8443").get("/health")
    assert secure.headers["strict-transport-security"] == "max-age=31536000"
    local = _client(app_client.app, base="https://localhost:8443", host="127.0.0.1").get("/health")
    assert "strict-transport-security" not in local.headers
    _running(monkeypatch, mode="testing")
    assert "strict-transport-security" not in _client(app_client.app, base="https://ib.example.org").get("/health").headers


def test_the_session_cookie_is_secure_over_https(app_client):
    login = {"username": "admin", "password": "Demo#Pass1"}
    over_https = _client(app_client.app, base="https://ib.example.org").post("/auth/login", json=login)
    over_http = _client(app_client.app, host="127.0.0.1").post("/auth/login", json=login)
    assert "secure" in over_https.headers["set-cookie"].lower()
    assert "secure" not in over_http.headers["set-cookie"].lower()


# --- the Server settings page -------------------------------------------------------------------

def test_only_admins(user_client):
    assert user_client.get("/server/settings").status_code == 403
    assert user_client.put("/server/ai", json={"provider": "off"}).status_code == 403


def test_saved_settings_win_and_need_a_restart(admin_client, monkeypatch, caplog):
    monkeypatch.setenv("PORT", "9000")
    server_settings.set_running(server_settings.effective(), "testing")
    shown = admin_client.get("/server/settings").json()
    assert shown["settings"]["http_port"] == 9000 and shown["sources"]["http_port"] == ".env"
    assert shown["restart_needed"] is False and "key_password" not in shown["settings"]
    caplog.set_level("INFO", logger="incident_bridge.audit")
    saved = admin_client.put("/server/settings", json={"http_port": 8080, "https_enabled": True,
                                                       "trusted_proxies": "10.0.0.5"}).json()
    assert saved["settings"]["http_port"] == 8080 and saved["sources"]["http_port"] == "page"
    assert saved["restart_needed"] is True and saved["settings"]["trusted_proxies"] == ["10.0.0.5/32"]
    record = next(r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "server.settings_changed")
    assert record["fields"] == ["http_port", "https_enabled", "trusted_proxies"]
    reset = admin_client.delete("/server/settings").json()
    assert reset["settings"]["http_port"] == 9000 and reset["settings"]["https_enabled"] is False


@pytest.mark.parametrize("body, message", [
    ({"https_enabled": True, "https_port": 8000}, "different ports"),
    ({"http_port": 0}, "port number"),
    ({"https_enabled": True, "certificate": "files", "cert_file": "/no/such.crt", "key_file": "/no/such.key"},
     "was not found"),
])
def test_unusable_settings_are_not_saved(admin_client, body, message):
    response = admin_client.put("/server/settings", json=body)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert admin_client.get("/server/settings").json()["sources"]["http_port"] == "default"


def test_production_refuses_the_generated_certificate(admin_client, monkeypatch, tmp_path, authority):
    monkeypatch.setenv("APP_MODE", "production")
    response = admin_client.put("/server/settings", json={"https_enabled": True, "https_only_remote": True})
    assert response.status_code == 400 and "testing only" in response.json()["detail"]
    assert admin_client.post("/server/certificate/generate").status_code == 400
    cert, key = _write(tmp_path, [authority["leaf"], authority["mid"]], authority["leaf_key"])
    checked = admin_client.post("/server/certificate/check", json={"cert_file": cert, "key_file": key}).json()
    assert not checked["ok"]                                     # the test authority is not a public one


def test_the_key_password_is_kept_and_never_shown(admin_client, tmp_path):
    info = tls.generate_self_signed(tmp_path, "pw")
    body = {"https_enabled": True, "certificate": "files", "cert_file": str(tmp_path / tls.GENERATED_CERT),
            "key_file": str(tmp_path / tls.GENERATED_KEY), "key_password": "pw"}
    saved = admin_client.put("/server/settings", json=body).json()
    assert saved["settings"]["key_password_set"] is True and "pw" not in str(saved)
    assert saved["certificate"]["ok"] and saved["certificate"]["info"]["fingerprint"] == info["fingerprint"]
    again = admin_client.put("/server/settings", json={"https_port": 9443}).json()   # password left as it was
    assert again["certificate"]["ok"]


def test_a_new_generated_certificate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        made = client.post("/server/certificate/generate").json()["info"]
        shown = client.put("/server/settings", json={"https_enabled": True}).json()["certificate"]
        assert shown["source"] == "generated" and shown["info"]["fingerprint"] == made["fingerprint"]
        assert (tmp_path / "tls" / tls.GENERATED_KEY).is_file()
        # running with it; a newer one waits for a restart
        server_settings.set_running(server_settings.effective({"https_enabled": True}), "testing", made["fingerprint"])
        assert client.get("/server/settings").json()["restart_needed"] is False
        client.post("/server/certificate/generate")
        assert client.get("/server/settings").json()["restart_needed"] is True


# --- AI settings after the setup -----------------------------------------------------------------

def test_the_ai_choice_applies_at_once(admin_client):
    on = admin_client.put("/server/ai", json={"provider": "fake"}).json()
    assert on["applied"] and on["source"] == "page" and on["status"].startswith("On") and on["running"]
    off = admin_client.put("/server/ai", json={"provider": "off"}).json()
    assert off["applied"] and "off" in off["status"] and not off["running"]
    assert admin_client.post("/server/ai/test", json={"provider": "fake"}).json()["ok"]
    env = admin_client.put("/server/ai", json={"provider": ""}).json()
    assert env["source"] == ".env"


def test_a_saved_api_key_is_kept_when_left_empty(admin_client):
    assert admin_client.put("/server/ai", json={"provider": "claude", "api_key": "sk-test",
                                                "models": "claude-x"}).status_code == 200
    kept = admin_client.put("/server/ai", json={"provider": "claude", "models": "claude-y"}).json()
    assert kept["has_key"] and kept["models"] == "claude-y" and "sk-test" not in str(kept)
    response = admin_client.put("/server/ai", json={"provider": "openai"})
    assert response.status_code == 400 and "API key" in response.json()["detail"]


# --- serve_app.py's plan -----------------------------------------------------------------------

@pytest.fixture
def serve_app(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import serve_app
    return serve_app


def test_before_the_setup_the_certificate_stays_out_of_the_data_folder(serve_app, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("HTTPS_ENABLED", "true")
    mode, settings, (cert, key, password) = serve_app.server_plan()
    assert mode == "testing" and settings.https_enabled and Path(cert).is_file()
    assert not (tmp_path / "data").exists()                     # the setup still finds it empty


def test_production_will_not_start_with_the_generated_certificate(serve_app, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("HTTPS_ENABLED", "true")
    monkeypatch.setenv("HTTPS_ONLY_REMOTE", "true")
    with pytest.raises(ValueError, match="testing only"):
        serve_app.server_plan()
    assert serve_app._proxy_options(ServerSettings()) == {"proxy_headers": False}
    assert serve_app._proxy_options(ServerSettings(trusted_proxies=["10.0.0.5/32"]))["forwarded_allow_ips"] == "10.0.0.5/32"


def test_the_certificate_made_before_the_start_is_not_reported_as_added_by_hand(serve_app, monkeypatch, tmp_path):
    """serve_app.py saves the generated key's password before the log runs; the app fingerprints it at start."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
    monkeypatch.setenv("HTTPS_ENABLED", "true")
    _, _, (cert, _, _) = serve_app.server_plan()
    assert Path(cert).parent == tmp_path / "tls"
    with TestClient(app) as client:
        client.post("/auth/login", json={"username": "admin", "password": "Demo#Pass1"})
        verify = client.get("/logs/verify").json()
        assert verify["ok"] is True, verify["data"]["problems"]

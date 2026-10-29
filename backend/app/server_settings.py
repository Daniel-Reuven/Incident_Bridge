"""
How the server listens: HTTP and HTTPS ports, "HTTPS only for remote
connections", the certificate and the trusted proxies.

    .env                 HOST, PORT, HTTPS_ENABLED, HTTPS_PORT, HTTPS_ONLY_REMOTE,
                         TLS_CERT_FILE, TLS_KEY_FILE, TLS_KEY_PASSWORD, TRUSTED_PROXIES
    Server settings page saved encrypted in the database ("server") and WINS over
                         .env, field by field; "Use .env" removes them again

Both are read by serve_app.py when it starts the server, so a change takes
effect at the next restart; the page shows what is running and what is
saved. HTTP from this computer itself (loopback) always stays open, so a
wrong setting can never lock the admin out.

APP_MODE (.env only - it describes the deployment, not a preference):
    testing     (default) anything goes, including the generated self-signed
                certificate (app/security/tls.py)
    production  HTTPS must be on, with certificate FILES that chain to a
                public certificate authority and have not expired, and remote
                HTTP is redirected to HTTPS. The app refuses to start otherwise.

RUNNING is what serve_app.py actually started (the redirect and HSTS follow
it - app/api/security.py); a bare `uvicorn app.api.app:app` leaves it at
"HTTP only".
"""

import ipaddress
import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional

MODES = ("testing", "production")
CERTIFICATE_SOURCES = ("generated", "files")
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTPS_PORT = 8443

# field -> .env name
ENV_NAMES = {
    "host": "HOST",
    "http_port": "PORT",
    "https_enabled": "HTTPS_ENABLED",
    "https_port": "HTTPS_PORT",
    "https_only_remote": "HTTPS_ONLY_REMOTE",
    "certificate": None,                     # "files" when TLS_CERT_FILE is set, else "generated"
    "cert_file": "TLS_CERT_FILE",
    "key_file": "TLS_KEY_FILE",
    "key_password": "TLS_KEY_PASSWORD",
    "trusted_proxies": "TRUSTED_PROXIES",
}
SECRET_FIELDS = ("key_password",)            # never sent to the page


@dataclass
class ServerSettings:
    host: str = "0.0.0.0"
    http_port: int = DEFAULT_HTTP_PORT
    https_enabled: bool = False
    https_port: int = DEFAULT_HTTPS_PORT
    https_only_remote: bool = False
    certificate: str = "generated"
    cert_file: str = ""
    key_file: str = ""
    key_password: str = ""
    trusted_proxies: List[str] = field(default_factory=list)

    def public(self) -> dict:
        """For the page: everything except the key password (only whether one is set)."""
        values = asdict(self)
        values["key_password_set"] = bool(values.pop("key_password"))
        return values


def app_mode() -> str:
    mode = (os.environ.get("APP_MODE") or "testing").strip().lower()
    if mode not in MODES:
        raise ValueError(f"APP_MODE must be testing or production (got {mode!r}).")
    return mode


def _bool(name: str, raw) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be true or false (got {raw!r}).")


def _port(name: str, raw) -> int:
    try:
        port = int(str(raw).strip())
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be a port number from 1 to 65535 (got {raw!r}).")
    return port


def _proxies(raw) -> List[str]:
    items = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
    result = []
    for item in (str(i).strip() for i in items):
        if not item:
            continue
        try:
            result.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ValueError(f"Trusted proxy {item!r} is not an IP address or network (e.g. 10.0.0.5 or "
                             f"10.0.0.0/24).") from None
    return result


def _coerce(name: str, raw):
    label = ENV_NAMES.get(name) or name
    if name in ("http_port", "https_port"):
        return _port(label, raw)
    if name in ("https_enabled", "https_only_remote"):
        return _bool(label, raw)
    if name == "trusted_proxies":
        return _proxies(raw)
    if name == "certificate":
        value = str(raw).strip().lower()
        if value not in CERTIFICATE_SOURCES:
            raise ValueError(f"The certificate must be generated or files (got {raw!r}).")
        return value
    return str(raw if raw is not None else "").strip()


def from_env() -> Dict[str, object]:
    """The fields .env sets (only those), coerced. ValueError for a bad value."""
    values: Dict[str, object] = {}
    for name, env in ENV_NAMES.items():
        raw = os.environ.get(env) if env else None
        if raw is not None and raw.strip() != "":
            values[name] = _coerce(name, raw)
    if values.get("cert_file"):
        values["certificate"] = "files"
    return values


def effective(stored: Optional[dict] = None) -> ServerSettings:
    """Defaults, then .env, then what the page saved. ValueError for a bad value."""
    values = asdict(ServerSettings())
    values.update(from_env())
    for name, raw in (stored or {}).items():
        if name in values:
            values[name] = _coerce(name, raw)
    return ServerSettings(**values)


def sources(stored: Optional[dict] = None) -> Dict[str, str]:
    """Where each field's value comes from: "page", ".env" or "default"."""
    env = from_env()
    return {f.name: "page" if f.name in (stored or {}) else ".env" if f.name in env else "default"
            for f in fields(ServerSettings)}


def coerce_changes(changes: dict) -> dict:
    """The page's values, checked one by one (ValueError names the bad one)."""
    return {name: _coerce(name, raw) for name, raw in changes.items() if name in ENV_NAMES}


def problems(settings: ServerSettings, mode: str) -> List[str]:
    """What stops these settings from being used (the certificate itself is checked by app/security/tls.py)."""
    found = []
    if settings.https_enabled and settings.https_port == settings.http_port:
        found.append("HTTP and HTTPS need different ports.")
    if settings.https_enabled and settings.certificate == "files" and not (settings.cert_file and settings.key_file):
        found.append("Give both the certificate file and its key file.")
    if settings.https_only_remote and not settings.https_enabled:
        found.append("\"HTTPS only for remote connections\" needs HTTPS turned on.")
    if mode == "production":
        if not settings.https_enabled:
            found.append("Production needs HTTPS: turn it on, with certificate files from a certificate authority.")
        elif settings.certificate != "files":
            found.append("Production needs certificate files from a certificate authority - the generated "
                         "self-signed certificate is for testing only.")
        if not settings.https_only_remote:
            found.append("Production needs \"HTTPS only for remote connections\" turned on.")
    return found


# --- what serve_app.py started --------------------------------------------------------------

@dataclass
class Running:
    http_port: Optional[int] = None
    https_port: Optional[int] = None            # None: no HTTPS listener
    https_only_remote: bool = False
    mode: str = "testing"
    settings: Optional[dict] = None             # the ServerSettings.public() it started with
    certificate: Optional[str] = None           # the fingerprint of the certificate it serves


RUNNING_ENV = "INCIDENT_BRIDGE_RUNNING"          # carries RUNNING into uvicorn's reload worker process


def _from_env() -> Running:
    try:
        return Running(**json.loads(os.environ[RUNNING_ENV]))
    except (KeyError, ValueError, TypeError):
        return Running()


RUNNING = _from_env()


def set_running(settings: ServerSettings, mode: str, certificate: Optional[str] = None) -> None:
    global RUNNING
    RUNNING = Running(http_port=settings.http_port, https_port=settings.https_port if settings.https_enabled else None,
                      https_only_remote=settings.https_only_remote and settings.https_enabled, mode=mode,
                      settings=settings.public(), certificate=certificate)
    os.environ[RUNNING_ENV] = json.dumps(asdict(RUNNING))

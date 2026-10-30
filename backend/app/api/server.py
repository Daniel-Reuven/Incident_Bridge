"""
The admin's Server settings page (frontend/server.html): HTTPS and the AI
analysis settings after the setup.

    GET    /server/settings              mode, saved + effective settings, where each comes
                                         from, what is running, the certificate
    PUT    /server/settings              save changes (they win over .env; applied at restart)
    DELETE /server/settings              forget the page's values - .env (or the defaults) again
    POST   /server/certificate/check     check certificate + key files (nothing saved)
    POST   /server/certificate/generate  a new self-signed certificate (testing only; at restart)
    GET    /server/ai                    the AI choice and whether analysis is on
    PUT    /server/ai                    save an AI choice - applied at once (the worker restarts)
    POST   /server/ai/test               a real call to the provider (nothing saved)

Network settings take effect when serve_app.py starts the server, so a
change is "saved - restart to apply"; HTTP from this computer always stays
open (app/server_settings.py). Secrets (the key password, API keys) are
stored encrypted and never sent back - only whether one is set.
"""

import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import server_settings
from app.ai import choice, stored_settings
from app.ai.config import AIConfig
from app.ai.providers import PROVIDERS
from app.api.deps import get_current_user
from app.audit import audit
from app.models import Role, User
from app.security import tls
from app.state import database_path

router = APIRouter(prefix="/server", tags=["server"])
SETTING = "server"


def get_server_admin(request: Request) -> User:
    user = get_current_user(request)
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Server settings are available to admins only.")
    return user


class ServerSettingsRequest(BaseModel):
    host: Optional[str] = None
    http_port: Optional[int] = None
    https_enabled: Optional[bool] = None
    https_port: Optional[int] = None
    https_only_remote: Optional[bool] = None
    certificate: Optional[str] = None
    cert_file: Optional[str] = None
    key_file: Optional[str] = None
    key_password: Optional[str] = None          # None: keep; "": none
    trusted_proxies: Optional[str] = None       # comma-separated


class CertificateCheckRequest(BaseModel):
    cert_file: str
    key_file: str
    key_password: Optional[str] = None          # None: the saved one


class AIRequest(BaseModel):
    provider: str                               # "" = use .env, "off", or a provider id
    api_key: Optional[str] = None               # empty: keep the saved key of the same provider
    models: Optional[str] = None
    gemini_tier: Optional[str] = None


def _settings(request: Request):
    return request.app.state.settings


def _certificate(settings: server_settings.ServerSettings, mode: str, store) -> dict:
    """The certificate the server uses (or will use), checked."""
    if settings.certificate == "generated":
        folder = tls.generated_dir(database_path()) if database_path() != ":memory:" else None
        cert = folder / tls.GENERATED_CERT if folder else None
        if cert is None or not cert.is_file():
            return {"source": "generated", "ok": mode != "production", "info": None, "warnings": [],
                    "problems": ["Production needs certificate files."] if mode == "production" else [],
                    "note": ("The certificate in use now was made just for the setup; the lasting one is made at "
                             "the next start." if server_settings.RUNNING.https_port
                             else "Made when the server starts with HTTPS on.")}
        password = (store.get(tls.SETTING) or {}).get("password", "")
        result = tls.check_files(str(cert), str(folder / tls.GENERATED_KEY), password, mode)
        return {"source": "generated", **result}
    if not (settings.cert_file and settings.key_file):
        return {"source": "files", "ok": False, "info": None, "warnings": [],
                "problems": ["No certificate files given yet."]}
    return {"source": "files", **tls.check_files(settings.cert_file, settings.key_file, settings.key_password, mode)}


def _report(request: Request) -> dict:
    store = _settings(request)
    mode = server_settings.app_mode()
    stored = store.get(SETTING) or {}
    effective = server_settings.effective(stored)
    running = server_settings.RUNNING
    return {
        "mode": mode,
        "settings": effective.public(),
        "sources": server_settings.sources(stored),
        "problems": server_settings.problems(effective, mode),
        "certificate": _certificate(effective, mode, store) if effective.https_enabled else None,
        "running": {"http_port": running.http_port, "https_port": running.https_port,
                    "https_only_remote": running.https_only_remote, "known": running.settings is not None},
        "restart_needed": running.settings is not None and (running.settings != effective.public() or _new_certificate(
            effective, store)),
    }


def _new_certificate(effective, store) -> bool:
    """HTTPS is running, and the certificate it would start with now is another one (e.g. a new generated one)."""
    running = server_settings.RUNNING
    if not (running.https_port and running.certificate and effective.https_enabled):
        return False
    try:
        if effective.certificate == "generated":
            path = tls.generated_dir(database_path()) / tls.GENERATED_CERT
        else:
            path = effective.cert_file
        return tls.certificate_info(path)["fingerprint"] != running.certificate
    except (OSError, ValueError):
        return False


@router.get("/settings")
def get_settings(request: Request, admin: User = Depends(get_server_admin)):
    return _report(request)


@router.put("/settings")
def save_settings(body: ServerSettingsRequest, request: Request, admin: User = Depends(get_server_admin)):
    store = _settings(request)
    stored = dict(store.get(SETTING) or {})
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    changes = server_settings.coerce_changes(changes)
    merged = {**stored, **changes}
    if merged.get("key_password") == "":
        merged.pop("key_password")
    effective = server_settings.effective(merged)
    mode = server_settings.app_mode()
    problems = server_settings.problems(effective, mode)
    if effective.https_enabled and effective.certificate == "files" and not problems:
        problems = tls.check_files(effective.cert_file, effective.key_file, effective.key_password, mode)["problems"]
    if problems:
        raise ValueError(" ".join(problems))
    changed = sorted(k for k in merged if stored.get(k) != merged.get(k)) + sorted(set(stored) - set(merged))
    store.set(SETTING, merged)
    audit("server.settings_changed", user=admin.username, fields=changed,
          message=f"{admin.username} changed the server settings ({', '.join(changed) or 'nothing'}) - "
                  f"applied at the next restart")
    return _report(request)


@router.delete("/settings")
def reset_settings(request: Request, admin: User = Depends(get_server_admin)):
    store = _settings(request)
    store.set(SETTING, {})
    audit("server.settings_reset", user=admin.username,
          message=f"{admin.username} reset the server settings to .env - applied at the next restart")
    return _report(request)


@router.post("/certificate/check")
def check_certificate(body: CertificateCheckRequest, request: Request, admin: User = Depends(get_server_admin)):
    password = body.key_password
    if password is None:
        password = server_settings.effective(_settings(request).get(SETTING) or {}).key_password
    return tls.check_files(body.cert_file.strip(), body.key_file.strip(), password, server_settings.app_mode())


@router.post("/certificate/generate")
def generate_certificate(request: Request, admin: User = Depends(get_server_admin)):
    if server_settings.app_mode() == "production":
        raise ValueError("Production uses certificate files from a certificate authority - no generated certificate.")
    info = tls.regenerate(database_path(), _settings(request))
    audit("server.certificate_generated", user=admin.username, fingerprint=info["fingerprint"],
          message=f"{admin.username} generated a new self-signed certificate - used at the next restart")
    return {"info": info}


# --- AI analysis ------------------------------------------------------------------------------

_ai_lock = threading.Lock()


def _ai_report(request: Request) -> dict:
    stored = stored_settings.stored()
    provider = stored.get("AI_PROVIDER")
    info = PROVIDERS.get(provider or "")
    try:
        config = AIConfig.from_env()
        status = f"On: {PROVIDERS[config.provider].display_name}" if config else AIConfig.off_reason()
    except ValueError as exc:
        status = f"Not usable: {exc}"
    worker = getattr(request.app.state, "analysis_worker", None)
    return {
        "providers": choice.provider_list(),
        "source": "page" if provider else ".env",
        "provider": provider or "",
        "has_key": bool(info and info.key_env and stored.get(info.key_env)),
        "models": stored.get(info.models_env, "") if info and info.models_env else "",
        "gemini_tier": stored.get("GEMINI_TIER", ""),
        "status": status,
        "running": worker is not None,
    }


def _values(body: AIRequest) -> dict:
    stored = stored_settings.stored()
    api_key = body.api_key
    info = PROVIDERS.get(body.provider)
    if info and info.key_env and not (api_key or "").strip() and stored.get("AI_PROVIDER") == body.provider:
        api_key = stored.get(info.key_env)              # the saved key of the same provider
    return choice.ai_values(body.provider, api_key, body.models, body.gemini_tier)


@router.get("/ai")
def get_ai(request: Request, admin: User = Depends(get_server_admin)):
    return _ai_report(request)


@router.post("/ai/test")
def test_ai(body: AIRequest, request: Request, admin: User = Depends(get_server_admin)):
    return choice.test_connection(_values(body))


@router.put("/ai")
def save_ai(body: AIRequest, request: Request, admin: User = Depends(get_server_admin)):
    values = _values(body)
    choice.ai_config(values)                            # ValueError for an unusable choice: nothing saved
    app = request.app
    with _ai_lock:
        _settings(request).set("ai", values)
        stored_settings.use(values)
        applied = _restart_worker(app)
    audit("server.ai_changed", user=admin.username, provider=values.get("AI_PROVIDER") or ".env",
          message=f"{admin.username} changed the AI analysis settings "
                  f"({values.get('AI_PROVIDER') or 'from .env'})" + ("" if applied else " - applied at the next restart"))
    return {**_ai_report(request), "applied": applied}


def _restart_worker(app) -> bool:
    """Swap the analysis worker for one with the new settings. False when the old one is still busy."""
    from app.ai.setup import create_analysis_worker
    from app.ai.worker import THREAD_NAME
    old = getattr(app.state, "analysis_worker", None)
    if old is not None:
        old.stop()
        if any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate()):
            return False                                # a call still running: the new settings wait for a restart
    app.state.analysis_worker = create_analysis_worker(app.state.incident_bridge, app.state.broadcaster,
                                                       database_path())
    return True

"""
The admin's Server settings page (frontend/server.html): every setting of
the app after the setup - kept encrypted in the database (app/app_config.py;
backend/.env only gave their initial values at setup).

    GET    /server/config                the general settings, grouped (saved value or default)
    PUT    /server/config                save changes ({name: value}; "" = the default) - at restart
    GET    /server/settings              HTTPS: mode, saved + effective settings, where each comes
                                         from, what is running, the certificate
    PUT    /server/settings              save HTTPS changes (applied at restart)
    DELETE /server/settings              HTTPS back to the defaults
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

import os
import threading
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import app_config, server_settings
from app.ai import choice, stored_settings
from app.ai.config import AIConfig
from app.ai.providers import PROVIDERS
from app.api.deps import get_current_user
from app.audit import audit
from app.models import Role, User
from app.security import tls
from app.state import database_path

router = APIRouter(prefix="/server", tags=["server"])


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


class ConfigRequest(BaseModel):
    values: Dict[str, Optional[str]]            # setting name -> value; "" or null: back to the default


class CertificateCheckRequest(BaseModel):
    cert_file: str
    key_file: str
    key_password: Optional[str] = None          # None: the saved one


class AIRequest(BaseModel):
    provider: str                               # "off" or a provider id
    api_key: Optional[str] = None               # empty: keep the saved key of the same provider
    models: Optional[str] = None
    gemini_tier: Optional[str] = None


def _settings(request: Request):
    return request.app.state.settings


def _mode(request: Request) -> str:
    return server_settings.app_mode(app_config.config(_settings(request)))


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
    saved = app_config.config(store)
    mode = server_settings.app_mode(saved)
    effective = server_settings.effective(base=saved)
    running = server_settings.RUNNING
    return {
        "mode": mode,
        "settings": effective.public(),
        "sources": server_settings.sources(saved),
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


@router.get("/config")
def get_config(request: Request, admin: User = Depends(get_server_admin)):
    return app_config.page(_settings(request))


@router.put("/config")
def save_config(body: ConfigRequest, request: Request, admin: User = Depends(get_server_admin)):
    changed = app_config.save(_settings(request), body.values)
    if changed:
        audit("server.config_changed", user=admin.username, fields=changed,
              message=f"{admin.username} changed the settings ({', '.join(changed)}) - applied at the next restart")
    return {**app_config.page(_settings(request)), "changed": changed}


@router.put("/settings")
def save_settings(body: ServerSettingsRequest, request: Request, admin: User = Depends(get_server_admin)):
    store = _settings(request)
    saved = app_config.config(store)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = {**saved, **server_settings.to_config(server_settings.coerce_changes(changes))}
    if updated.get("TLS_KEY_PASSWORD") == "":
        updated.pop("TLS_KEY_PASSWORD")
    if updated.get("TRUSTED_PROXIES") == "":
        updated.pop("TRUSTED_PROXIES")
    effective = server_settings.effective(base=updated)
    mode = server_settings.app_mode(updated)
    problems = server_settings.problems(effective, mode)
    if effective.https_enabled and effective.certificate == "files" and not problems:
        problems = tls.check_files(effective.cert_file, effective.key_file, effective.key_password, mode)["problems"]
    if problems:
        raise ValueError(" ".join(problems))
    changed = sorted(field for field, name in server_settings.ENV_NAMES.items()
                     if saved.get(name) != updated.get(name))
    store.set(app_config.CONFIG_KEY, updated)
    audit("server.settings_changed", user=admin.username, fields=changed,
          message=f"{admin.username} changed the HTTPS settings ({', '.join(changed) or 'nothing'}) - "
                  f"applied at the next restart")
    return _report(request)


@router.delete("/settings")
def reset_settings(request: Request, admin: User = Depends(get_server_admin)):
    store = _settings(request)
    saved = app_config.config(store)
    remaining = {k: v for k, v in saved.items() if k not in app_config.SERVER_NAMES}
    if server_settings.problems(server_settings.effective(base=remaining), server_settings.app_mode(remaining)):
        raise ValueError("Production mode needs these HTTPS settings - switch the mode to testing first.")
    store.set(app_config.CONFIG_KEY, remaining)
    audit("server.settings_reset", user=admin.username,
          message=f"{admin.username} put the HTTPS settings back to the defaults - applied at the next restart")
    return _report(request)


@router.post("/certificate/check")
def check_certificate(body: CertificateCheckRequest, request: Request, admin: User = Depends(get_server_admin)):
    password = body.key_password
    if password is None:
        password = server_settings.effective(base=app_config.config(_settings(request))).key_password
    return tls.check_files(body.cert_file.strip(), body.key_file.strip(), password, _mode(request))


@router.post("/certificate/generate")
def generate_certificate(request: Request, admin: User = Depends(get_server_admin)):
    if _mode(request) == "production":
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
    key_source = None
    if info and info.key_env:
        key_source = "saved" if stored.get(info.key_env) else ".env" if os.environ.get(info.key_env) else None
    return {
        "providers": choice.provider_list(),
        "provider": provider or "off",
        "has_key": key_source is not None,
        "key_source": key_source,                       # ".env": not saved here - read from backend/.env
        "models": stored.get(info.models_env, "") if info and info.models_env else "",
        "gemini_tier": stored.get("GEMINI_TIER", ""),
        "status": status,
        "running": worker is not None,
    }


def _values(body: AIRequest, for_saving: bool = False) -> dict:
    """
    The AI choice as settings. No key typed: the saved key of the same
    provider, else the one in backend/.env - which is then not saved, so it
    stays read from .env (the course brief: the key comes from the environment).
    """
    stored = stored_settings.stored()
    provider = (body.provider or "").strip().lower() or "off"
    api_key = body.api_key
    info = PROVIDERS.get(provider)
    from_env = False
    if info and info.key_env and not (api_key or "").strip():
        if stored.get("AI_PROVIDER") == provider and stored.get(info.key_env):
            api_key = stored[info.key_env]              # the saved key of the same provider
        elif os.environ.get(info.key_env):
            api_key, from_env = os.environ[info.key_env], True
    values = choice.ai_values(provider, api_key, body.models, body.gemini_tier)
    if for_saving and from_env:
        values.pop(info.key_env, None)
    return values


@router.get("/ai")
def get_ai(request: Request, admin: User = Depends(get_server_admin)):
    return _ai_report(request)


@router.post("/ai/test")
def test_ai(body: AIRequest, request: Request, admin: User = Depends(get_server_admin)):
    return choice.test_connection(_values(body))


@router.put("/ai")
def save_ai(body: AIRequest, request: Request, admin: User = Depends(get_server_admin)):
    choice.ai_config(_values(body))                     # ValueError for an unusable choice: nothing saved
    values = _values(body, for_saving=True)
    app = request.app
    with _ai_lock:
        _settings(request).set("ai", values)
        stored_settings.use(values)
        applied = _restart_worker(app)
    audit("server.ai_changed", user=admin.username, provider=values["AI_PROVIDER"],
          message=f"{admin.username} changed the AI analysis settings "
                  f"({values['AI_PROVIDER']})" + ("" if applied else " - applied at the next restart"))
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

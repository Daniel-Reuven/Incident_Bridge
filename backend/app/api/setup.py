"""
The first-start setup (frontend/setup.html): the only thing a new
installation does until it is finished.

    1. The console prints a one-time SETUP CODE; every setup request must
       carry it, so nobody else on the network can take over a fresh install
       (10 wrong codes print a new one).
    2. POST /setup/start      the keys are generated (app/security/installation.py)
                              and the RECOVERY KEY is shown - once.
    3. POST /setup/test-ai    optional: check an AI provider key with a real GET
                              of the provider's model list.
    4. POST /setup/complete   the admin types back the recovery key's last two
                              groups (proof it was copied), chooses the first
                              admin account and, optionally, the AI settings.
                              Then: keys saved, database created encrypted, admin
                              and the regular users of INCIDENT_BRIDGE_USERS
                              created, secrets generated, services started.

If the keys already exist but no admin does (a setup interrupted after the
keys were saved), the setup runs again without a new recovery key.

SetupGateMiddleware holds back everything else while the setup is pending:
pages are redirected to /setup.html, API calls get 503; the setup page's
own files, /health and the public pages stay reachable.
"""

import hmac
import re
import secrets
import threading
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.ai import stored_settings
from app.ai.config import AIConfig
from app.ai.providers import PROVIDERS, build_provider
from app.api.lifecycle import start_services
from app.api_client import ApiError
from app.audit import audit
from app.logs import app_log
from app.models import Role, User
from app.models.system_users import is_reserved_username
from app.repository import UserProvisioningError, create_initial_users, seed_users_from_env
from app.security.data_folders import check_new, write_markers
from app.security.installation import Installation
from app.security.key_providers import FileProvider, KeyProviderError, choose_provider
from app.settings_store import SqliteSettingsStore
from app.state import database_path

router = APIRouter(prefix="/setup", tags=["setup"])

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O or 1/I
MAX_WRONG_CODES = 10


class SetupSession:
    """The setup in progress: its code, and the keys made by /setup/start (kept in memory until complete)."""

    def __init__(self, installation: Installation, settings: Optional[SqliteSettingsStore] = None):
        self.installation = installation
        self.settings = settings                # set when the keys already exist (accounts-only setup)
        self.lock = threading.Lock()
        self.pending = None                     # (recovery key text, database key, keys-file data, provider)
        self.wrong_codes = 0
        self.code = ""
        self.new_code()

    @property
    def keys_exist(self) -> bool:
        return self.settings is not None

    def new_code(self) -> None:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self.code = f"{raw[:4]}-{raw[4:]}"
        self.wrong_codes = 0
        line = "=" * 64
        app_log.warning(f"\n{line}\n  Incident Bridge is not set up yet.\n  Open /setup.html in a browser and enter "
                        f"this setup code:   {self.code}\n{line}")

    def check(self, code: Optional[str]) -> None:
        given = (code or "").strip().upper().replace(" ", "")
        if hmac.compare_digest(given.encode(), self.code.encode()):
            return
        self.wrong_codes += 1
        if self.wrong_codes >= MAX_WRONG_CODES:
            self.new_code()
            raise PermissionError("Too many wrong setup codes - a new code was printed on the server console.")
        raise PermissionError("Wrong setup code - it is printed on the server console.")


class StartRequest(BaseModel):
    code: str


class AISettings(BaseModel):
    provider: str                       # gemini | claude | openai | fake
    api_key: Optional[str] = None
    models: Optional[str] = None        # comma-separated, best first
    gemini_tier: Optional[str] = None   # free | paid


class TestAIRequest(BaseModel):
    code: str
    ai: AISettings


class CompleteRequest(BaseModel):
    code: str
    recovery_check: Optional[str] = None
    admin_username: str
    admin_password: str
    confirm_password: str
    admin_first_name: str = ""          # optional: the admin's display name (app/display_names.py)
    admin_last_name: str = ""
    admin_job_title: str = ""
    ai: Optional[AISettings] = None


def _session(request: Request) -> SetupSession:
    session = getattr(request.app.state, "setup", None)
    if session is None:
        raise RuntimeError("Incident Bridge is already set up.")             # 409
    return session


def _ai_values(ai: Optional[AISettings]) -> dict:
    """The AI choice as .env-style names (how app/ai/config.py reads it). {} = no AI settings stored."""
    if ai is None or ai.provider in ("", "none"):
        return {}
    if ai.provider not in PROVIDERS:
        raise ValueError(f"Unknown AI provider {ai.provider!r} - use one of: {', '.join(PROVIDERS)}.")
    info = PROVIDERS[ai.provider]
    values = {"AI_PROVIDER": ai.provider}
    if info.key_env:
        if not (ai.api_key or "").strip():
            raise ValueError(f"Enter the {info.display_name} API key, or choose no AI analysis for now.")
        values[info.key_env] = ai.api_key.strip()
    if info.models_env and (ai.models or "").strip():
        values[info.models_env] = ai.models.strip()
    if ai.provider == "gemini" and ai.gemini_tier:
        values["GEMINI_TIER"] = ai.gemini_tier.strip()
    return values


def _ai_config(values: dict) -> Optional[AIConfig]:
    """The AIConfig these values give (ValueError for an invalid one, e.g. a key without a model)."""
    with stored_settings.trying(values):
        return AIConfig.from_env()


def _key_store_preview() -> dict:
    try:
        provider = choose_provider()
    except ValueError as exc:
        return {"name": None, "description": str(exc), "warning": str(exc)}
    warning = provider.warning("", {}) if isinstance(provider, FileProvider) else None
    return {"name": provider.name, "description": provider.preview(), "warning": warning}


@router.get("/status")
def setup_status(request: Request):
    session = getattr(request.app.state, "setup", None)
    return {"setup_required": session is not None, "keys_exist": bool(session and session.keys_exist)}


@router.post("/start")
def start(body: StartRequest, request: Request):
    session = _session(request)
    session.check(body.code)
    with session.lock:
        if not session.keys_exist and session.pending is None:
            recovery, db_key, data = session.installation.create()
            session.pending = (recovery, db_key, data)
        try:
            regular = [u.username for u in seed_users_from_env(warn=False).accounts() if u.role == Role.USER]
            users_problem = None
        except UserProvisioningError as exc:
            regular, users_problem = [], str(exc)
        env_ai = None
        with stored_settings.trying({}):
            try:
                config = AIConfig.from_env()
                env_ai = {"provider": PROVIDERS[config.provider].display_name} if config else None
            except ValueError:
                env_ai = None
        folders = [] if session.keys_exist else check_new(session.installation.folders())
        return {
            "folders": folders,
            "folders_ok": all(f["ok"] for f in folders),
            "recovery_key": session.pending[0] if session.pending else None,
            "fingerprint": session.pending[2]["fingerprint"] if session.pending else None,
            "keys_exist": session.keys_exist,
            "key_store": _key_store_preview(),
            "regular_users": regular,
            "users_problem": users_problem,
            "ai_from_env": env_ai,
            "providers": [{"id": key, "name": info.display_name, "needs_key": bool(info.key_env),
                           "models_setting": info.models_env} for key, info in PROVIDERS.items()],
        }


@router.post("/test-ai")
def test_ai(body: TestAIRequest, request: Request):
    """Check an AI provider key with a real GET of its model list (nothing is saved)."""
    _session(request).check(body.code)
    config = _ai_config(_ai_values(body.ai))
    if config is None:
        raise ValueError("Enter an API key to test.")
    try:
        models = build_provider(config).list_models()
    except ApiError as exc:
        return {"ok": False, "detail": f"{PROVIDERS[config.provider].display_name} refused or could not be reached: "
                                       f"{exc}"}
    return {"ok": True, "models": models[:25],
            "detail": f"Connected to {PROVIDERS[config.provider].display_name}: {len(models)} model(s) available."}


@router.post("/complete")
async def complete(body: CompleteRequest, request: Request):
    app = request.app
    session = _session(request)
    session.check(body.code)
    with session.lock:
        # --- check everything first, so a refusal changes nothing ---------------------------
        if not session.keys_exist:
            crowded = [f for f in check_new(session.installation.folders()) if not f["ok"]]
            if crowded:
                raise ValueError("These folders must be empty (or not exist) before the setup - empty them, or "
                                 "set another path in .env and restart: "
                                 + "; ".join(f"{f['path']} ({f['role']}): {f['problem']}" for f in crowded))
            if session.pending is None:
                raise RuntimeError("Start the setup first - the recovery key has not been shown yet.")
            groups = session.pending[0].split("-")
            expected = "".join(groups[-2:])
            given = "".join(ch for ch in (body.recovery_check or "").upper() if ch.isalnum())
            if not hmac.compare_digest(given.encode(), expected.encode()):
                raise ValueError("The last two groups of the recovery key do not match - copy them from the key "
                                 "you saved.")
        username = body.admin_username.strip()
        if not USERNAME_PATTERN.match(username):
            raise ValueError("Username must be 3-32 characters: letters, digits, '.', '_' or '-'.")
        if is_reserved_username(username):
            raise ValueError(f"{username!r} is reserved for a built-in system user.")
        if body.admin_password != body.confirm_password:
            raise ValueError("The password and its confirmation do not match.")
        admin = User(username, Role.ADMIN, body.admin_password)          # PasswordPolicy: ValueError
        admin.set_details({"first_name": body.admin_first_name, "last_name": body.admin_last_name,
                           "job_title": body.admin_job_title})
        try:
            seed_users_from_env(warn=False)
        except UserProvisioningError as exc:
            raise ValueError(str(exc)) from None
        ai_values = _ai_values(body.ai)
        _ai_config(ai_values)

        # --- then do it -----------------------------------------------------------------------
        if session.keys_exist:
            settings = session.settings
            key_store = None
        else:
            _, db_key, data = session.pending
            try:
                provider = session.installation.save_new(db_key, data)
            except KeyProviderError as exc:
                raise RuntimeError(f"The key could not be stored: {exc}") from None
            settings = SqliteSettingsStore(database_path())
            key_store = session.installation.describe()["machine_key"]
        if ai_values:
            settings.set("ai", ai_values)
        await start_services(app, settings)
        try:
            regular = create_initial_users(app.state.incident_bridge.users, admin)
        except UserProvisioningError as exc:
            raise ValueError(str(exc)) from None
        app.state.setup = None
        session.pending = None
        # Mark the folders as this installation's (checked at every start - app/security/data_folders.py).
        write_markers(session.installation.folders(), settings.get("installation")["id"])

    audit("setup.completed", user=admin.username, key_store=key_store,
          regular_users=[u.username for u in regular], ai_provider=ai_values.get("AI_PROVIDER"),
          message=f"Setup completed by {admin.username}: {len(regular)} regular user(s) created")
    warning = session.installation.machine_warning()
    if warning:
        app_log.warning(f"Key store: {warning}")
    return {"detail": "Incident Bridge is ready - log in with the admin account you just created.",
            "admin": admin.username, "regular_users": [u.username for u in regular],
            "key_store": key_store, "key_store_warning": warning,
            "folders": [{"role": role, "path": str(folder)} for role, folder in session.installation.folders()]}


# --- holding back everything else until the setup is done ----------------------------------

_SETUP_OPEN = ("/setup", "/css/", "/js/", "/screenshots/", "/health", "/auth/password-policy", "/guide.html", "/about.html",
               "/terms.html", "/favicon.ico")


class SetupGateMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pending = getattr(scope["app"].state, "setup", None) is not None
        path = scope.get("path", "")
        if not pending:
            if path == "/setup.html":
                await RedirectResponse("/", status_code=307)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if path.startswith(_SETUP_OPEN):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "GET" and (path == "/" or path.endswith(".html")):
            response = RedirectResponse("/setup.html", status_code=307)
        else:
            response = JSONResponse(status_code=503, content={
                "detail": "Incident Bridge is not set up yet - finish the setup at /setup.html.",
                "setup_required": True})
        await response(scope, receive, send)


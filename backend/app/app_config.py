"""
The app's settings, kept in the encrypted database and managed on the
admin's Server settings page - backend/.env only supplies their INITIAL
values.

    setup (or the first start of an installation made before this)
        every setting below that backend/.env (or the environment) sets is
        copied into the encrypted settings, "config" - once
    every start
        the saved values are what the app uses: apply() puts them in the
        process environment, where every module reads its settings, and
        removes any other value of these names - so backend/.env no longer
        changes them
    the Server settings page
        shows and saves them; a change takes effect at the next restart

The AI provider, its API key and models are kept the same way, in "ai"
(app/ai/stored_settings.py), and copied from .env at setup too. One
exception, so that the course brief's rule holds (the external service's key
comes from the environment): an API key that is NOT saved in the app is
still read from backend/.env.

What stays in backend/.env - read before the encrypted database can be
opened, or only by the setup or the developer's server process: see
BOOTSTRAP.
"""

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple

CONFIG_KEY = "config"           # app_settings row: {setting name: value as text}

# Read before the database is unlocked (where it is, how its key is kept), or
# only by the setup / the developer's server process - these stay in .env.
BOOTSTRAP = ("DATA_DIR", "DATABASE_PATH", "DATABASE_KEYS_PATH", "LOG_DIR", "LOG_ARCHIVE_DIR", "KEY_PROVIDER",
             "MACHINE_KEY_DIR", "INCIDENT_BRIDGE_USERS", "RELOAD", "ALLOWED_ORIGINS")

# The AI choice ("ai" in the settings, app/ai/stored_settings.py) - copied at setup like the rest.
AI_NAMES = ("AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_TIER", "GEMINI_MODELS", "GEMINI_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL")

# The HTTPS settings - edited in the page's HTTPS section (app/server_settings.py, app/api/server.py).
SERVER_NAMES = ("HOST", "PORT", "HTTPS_ENABLED", "HTTPS_PORT", "HTTPS_ONLY_REMOTE", "TLS_CERTIFICATE",
                "TLS_CERT_FILE", "TLS_KEY_FILE", "TLS_KEY_PASSWORD", "TRUSTED_PROXIES")


@dataclass(frozen=True)
class Setting:
    name: str
    group: str
    label: str
    default: str
    help: str
    choices: Tuple[str, ...] = ()           # a fixed list (shown as a menu)
    kind: str = "text"                      # "bool" | "number" | "choice" | "text"
    check: Optional[Callable[[], object]] = field(default=None, compare=False)   # reads the environment


def _checks():
    """The functions that read (and validate) each setting - imported late: they import much of the app."""
    from app.ai.config import _read_number
    from app.ai.log import log_level_from_env
    from app.ai.providers.gemini_models import gemini_settings
    from app.api.security import session_settings_from_env
    from app.archive_rules import request_archive_after_from_env
    from app.audit import log_dirs_from_env
    from app.ip_privacy import mode_from_env
    from app.repository import first_login_change_required
    from app.server_settings import app_mode
    from app.services.availability import HttpChecker
    from app.sites import notification_archive_after_from_env
    return {
        "REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN": first_login_change_required,
        "SESSION_MAX_AGE_HOURS": session_settings_from_env,
        "SESSION_COOKIE_SECURE": session_settings_from_env,
        "LOG_FILE_ENABLED": log_dirs_from_env,
        "LOG_IP_MODE": mode_from_env,
        "SITE_CHECK_TIMEOUT_SECONDS": HttpChecker.from_env,
        "SITE_CHECK_SLOW_MS": HttpChecker.from_env,
        "NOTIFICATION_ARCHIVE_DAYS": notification_archive_after_from_env,
        "AI_REQUEST_TIMEOUT_SECONDS": lambda: _read_number("AI_REQUEST_TIMEOUT_SECONDS", 180, allow_zero=False),
        "AI_REQUEST_DELAY_SECONDS": lambda: _read_number("AI_REQUEST_DELAY_SECONDS", 5, allow_zero=True),
        "AI_REANALYSIS_COOLDOWN_SECONDS": lambda: _read_number("AI_REANALYSIS_COOLDOWN_SECONDS", 300, allow_zero=True),
        "AI_REQUEST_ARCHIVE_DAYS": request_archive_after_from_env,
        "AI_LOG_LEVEL": log_level_from_env,
        "GEMINI_MODEL_DISCOVERY": gemini_settings,
        "APP_MODE": app_mode,
    }


SETTINGS: Tuple[Setting, ...] = (
    Setting("APP_MODE", "Server", "Mode", "testing",
            "Testing allows the generated self-signed certificate. Production needs HTTPS with a certificate from a "
            "certificate authority and HTTPS only for remote connections - the server will not start otherwise.",
            choices=("testing", "production"), kind="choice"),
    Setting("REQUIRE_PASSWORD_CHANGE_ON_FIRST_LOGIN", "Accounts and logins", "Password chosen by someone else must be "
            "changed at the next login", "true",
            "New accounts and passwords reset by an admin. Off: the person keeps the password they were given.",
            kind="bool"),
    Setting("SESSION_MAX_AGE_HOURS", "Accounts and logins", "A login lasts at most (hours)", "12",
            "After this, the person must log in again.", kind="number"),
    Setting("SESSION_COOKIE_SECURE", "Accounts and logins", "Send the login cookie over HTTPS only, always", "false",
            "Off: the cookie is HTTPS-only whenever the login came over HTTPS. On: always - e.g. behind a proxy that "
            "does HTTPS but forwards plain HTTP.", kind="bool"),
    Setting("LOG_FILE_ENABLED", "Activity log", "Write the activity log", "true",
            "Off: no log files are written (Check integrity then has nothing to check).", kind="bool"),
    Setting("LOG_IP_MODE", "Activity log", "Client addresses in the log", "hashed",
            "hashed: a code that cannot be turned back into the address; masked: the last part only; full: as is; "
            "off: not at all.", choices=("hashed", "masked", "full", "off"), kind="choice"),
    Setting("SITE_CHECK_TIMEOUT_SECONDS", "Site portal", "Site check timeout (seconds)", "5",
            "How long a site check waits for an answer.", kind="number"),
    Setting("SITE_CHECK_SLOW_MS", "Site portal", "A site answering slower than this is Degraded (milliseconds)",
            "2000", "", kind="number"),
    Setting("NOTIFICATION_ARCHIVE_DAYS", "Site portal", "Finished notifications move to the archive after (days)",
            "30", "0 = never. Notifications waiting for a decision never do.", kind="number"),
    Setting("AI_REQUEST_ARCHIVE_DAYS", "AI analysis", "Finished AI requests move to the archive after (days)", "30",
            "0 = never. Archived requests no longer count in the site report.", kind="number"),
    Setting("AI_REQUEST_TIMEOUT_SECONDS", "AI analysis", "Time limit for one AI call (seconds)", "180", "",
            kind="number"),
    Setting("AI_REQUEST_DELAY_SECONDS", "AI analysis", "Pause after each AI call (seconds)", "5",
            "Keeps within the provider's request limits.", kind="number"),
    Setting("AI_REANALYSIS_COOLDOWN_SECONDS", "AI analysis", "An admin may re-analyze an unchanged fault after "
            "(seconds)", "300", "", kind="number"),
    Setting("AI_LOG_LEVEL", "AI analysis", "Console log of the AI queue", "INFO",
            "INFO: every step; WARNING: problems only; DEBUG: adds the pauses; OFF: nothing.",
            choices=("INFO", "WARNING", "DEBUG", "OFF"), kind="choice"),
    Setting("GEMINI_MODEL_DISCOVERY", "AI analysis", "Update Gemini's model list from Google", "off",
            "auto: when the AI worker starts, newer models are added and retired ones removed.",
            choices=("off", "auto"), kind="choice"),
)
BY_NAME: Dict[str, Setting] = {s.name: s for s in SETTINGS}

# Every name this module puts in the process environment at start (and removes when it is not saved).
APPLIED_NAMES = tuple(BY_NAME) + SERVER_NAMES

_env_lock = threading.Lock()
_applied: Optional[Dict[str, str]] = None          # what apply() put in place at this start
_unrecorded: List[str] = []                        # settings rows initialize() wrote before the log ran


def config(store) -> Dict[str, str]:
    return dict(store.get(CONFIG_KEY) or {})


def _from_environment(names) -> Dict[str, str]:
    return {name: os.environ[name].strip() for name in names if (os.environ.get(name) or "").strip()}


def initialize(store) -> bool:
    """
    Copy the initial values from backend/.env (the environment) into the
    encrypted settings - once per installation: at setup, or at the first start
    of an installation made before this. Also carries over the HTTPS values an
    older version saved on the page ("server"). Returns True when it copied.
    """
    if store.get(CONFIG_KEY) is not None:
        return False
    values = _from_environment(APPLIED_NAMES)
    if values.get("TLS_CERT_FILE") and "TLS_CERTIFICATE" not in values:
        values["TLS_CERTIFICATE"] = "files"
    from app.server_settings import to_config
    values.update(to_config(store.get("server") or {}))          # an older version's page values win
    store.set(CONFIG_KEY, values)
    _unrecorded.append(CONFIG_KEY)
    if store.get("ai") is None:                                   # nothing chosen at setup: .env's AI settings
        store.set("ai", _from_environment(AI_NAMES))
        _unrecorded.append("ai")
    return True


def take_unrecorded() -> List[str]:
    """
    The rows initialize() wrote before the activity log was running - once:
    app/api/lifecycle.py fingerprints them when the log starts, so Check
    integrity does not report them as added by hand.
    """
    names = list(dict.fromkeys(_unrecorded))
    _unrecorded.clear()
    return names


def apply(store) -> Dict[str, str]:
    """
    Make the saved settings the ones the app uses (every module reads them from
    the environment): each saved value is set, every other value of these names
    removed - backend/.env no longer changes them. Called at every start, before
    the services start. Also limits the AI settings to the saved ones (the API
    keys excepted - app/ai/stored_settings.py).
    An installation that has no saved settings yet (made before this) uses
    what initialize() would copy - serve_app.py applies before the app starts,
    and the app's own start then saves them.
    """
    global _applied
    saved = store.get(CONFIG_KEY)
    values = dict(saved) if saved is not None else _from_environment(APPLIED_NAMES)
    with _env_lock:
        for name in APPLIED_NAMES:
            if name in values and str(values[name]).strip() != "":
                os.environ[name] = str(values[name])
            else:
                os.environ.pop(name, None)
        _applied = {name: values[name] for name in APPLIED_NAMES if name in values}
    from app.ai import stored_settings
    stored_settings.saved_only(True)
    return dict(_applied)


def applied() -> Optional[Dict[str, str]]:
    """What apply() put in place at this start (None: not applied - e.g. under a bare test app)."""
    return None if _applied is None else dict(_applied)


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    """The environment as if `values` were applied (names in APPLIED_NAMES only), for the duration of the block."""
    with _env_lock:
        saved = {name: os.environ.get(name) for name in APPLIED_NAMES}
        try:
            for name in APPLIED_NAMES:
                if str(values.get(name, "")).strip():
                    os.environ[name] = str(values[name])
                else:
                    os.environ.pop(name, None)
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def validate(values: Mapping[str, str]) -> None:
    """ValueError (naming the setting) if any value in `values` would stop the app from starting."""
    checks = _checks()
    with _environment(values):
        for name in sorted({n for n in values if n in checks}, key=list(BY_NAME).index):
            checks[name]()
    if (values.get("APP_MODE") or "testing").strip().lower() == "production":
        from app.server_settings import effective, problems
        found = problems(effective(base=values), "production")
        if found:
            raise ValueError("Production mode cannot be used yet: " + " ".join(found))


def page(store) -> dict:
    """The generic settings for the Server settings page: grouped, with the saved value or the default."""
    saved = config(store)
    applied_now = applied()
    groups: Dict[str, List[dict]] = {}
    for s in SETTINGS:
        groups.setdefault(s.group, []).append({
            "name": s.name, "label": s.label, "help": s.help, "kind": s.kind, "choices": list(s.choices),
            "default": s.default, "value": saved.get(s.name), "saved": s.name in saved})
    restart = applied_now is not None and any(saved.get(n) != applied_now.get(n) for n in BY_NAME)
    return {"groups": [{"name": name, "settings": items} for name, items in groups.items()],
            "restart_needed": restart}


def save(store, changes: Mapping[str, Optional[str]]) -> List[str]:
    """
    Save the page's changes ({name: value}; "" or None: back to the default).
    Validates the whole result first - nothing is saved if a value is wrong.
    Returns the names that changed.
    """
    unknown = sorted(set(changes) - set(BY_NAME))
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}.")
    current = config(store)
    updated = dict(current)
    for name, value in changes.items():
        text = "" if value is None else str(value).strip()
        if BY_NAME[name].kind == "bool" and text:
            text = "true" if text.lower() in ("true", "1", "yes", "on") else "false" if text.lower() in (
                "false", "0", "no", "off") else text
        if text:
            updated[name] = text
        else:
            updated.pop(name, None)
    validate(updated)
    changed = sorted(n for n in BY_NAME if current.get(n) != updated.get(n))
    if changed:
        store.set(CONFIG_KEY, updated)
    return changed

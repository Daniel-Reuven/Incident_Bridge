"""
How client IP addresses appear in the logs - the activity log files
(app/audit.py) and the console's HTTP lines (app/logs.py). An address can
identify a person, so the operator chooses (LOG_IP_MODE in backend/.env):

    hashed   (default) a keyed pseudonym, e.g. "ip-3f9a1c0b7d2e": the same
             address always gives the same token, so repeated failed logins
             or a burst of requests from one place can still be followed -
             but the address cannot be read back. The token is an HMAC with
             the log signing key (app/audit.py's signing_key()), so it cannot
             be reversed by hashing all 4 billion IPv4 addresses either.
    masked   only the last part kept: "192.168.0.1" -> "***.***.***.1",
             "2001:db8::42" -> "****::42" (anything else -> "***"). Readable,
             but many addresses share one value.
    full     the address as is - for troubleshooting on a private network.
    off      no address at all (null in the log, "-" on the console).

Only the logs are affected: the login lockout (app/services/login_guard.py)
still counts failures per real address, in memory.
"""

import hashlib
import hmac
import ipaddress
import os
from typing import Optional

MODES = ("hashed", "masked", "full", "off")
DEFAULT_MODE = "hashed"
_HASH_LENGTH = 12          # hex digits: 48 bits - collisions are negligible for one installation


def mode_from_env() -> str:
    """LOG_IP_MODE (default "hashed"). Raises ValueError for an unknown value."""
    mode = (os.environ.get("LOG_IP_MODE") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise ValueError(f"LOG_IP_MODE must be one of {', '.join(MODES)} (got {mode!r}).")
    return mode


def mask(ip: str) -> str:
    """Keep only the last part of the address (see the module docstring)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "***"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        return "***.***.***." + str(address).rsplit(".", 1)[1]
    last = address.exploded.rsplit(":", 1)[1].lstrip("0") or "0"
    return f"****::{last}"


def pseudonym(ip: str, key: bytes) -> str:
    """A stable token for `ip` that cannot be turned back into it without `key`."""
    digest = hmac.new(key, b"ip|" + ip.encode("utf-8"), hashlib.sha256).hexdigest()
    return "ip-" + digest[:_HASH_LENGTH]


class IpPrivacy:
    """Applies one mode. `key` is only used by "hashed"."""

    def __init__(self, mode: str, key: bytes):
        if mode not in MODES:
            raise ValueError(f"unknown IP mode {mode!r}")
        self.mode = mode
        self._key = key

    def apply(self, ip: Optional[str]) -> Optional[str]:
        """The address as the logs may show it (None stays None)."""
        if not ip or self.mode == "full":
            return ip
        if self.mode == "off":
            return None
        if self.mode == "masked":
            return mask(ip)
        return pseudonym(ip, self._key)


_current: Optional[IpPrivacy] = None


def configure(mode: str, key: bytes) -> IpPrivacy:
    """Set the mode for the whole process - called at startup by app/audit.py's configure_audit_log()."""
    global _current
    _current = IpPrivacy(mode, key)
    return _current


def current() -> IpPrivacy:
    """The configured mode; before startup has configured it, the settings' (or the default if they are invalid)."""
    if _current is not None:
        return _current
    from app.audit import signing_key          # here: app.audit imports this module
    try:
        mode = mode_from_env()
    except ValueError:
        mode = DEFAULT_MODE
    return configure(mode, signing_key())

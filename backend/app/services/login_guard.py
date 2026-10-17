"""
LoginGuard: limits password guessing at POST /auth/login.

Failed logins are counted per USERNAME and per client ADDRESS within a
sliding window. When either count reaches its limit, that username or
address is locked for LOCKOUT, and the login endpoint answers 429 with a
Retry-After header - even for the right password - until it expires.

    per username:  5 failures in 15 minutes -> locked for 15 minutes
    per address:  20 failures in 15 minutes -> locked for 15 minutes
                   (higher, because several people may share one address)

A successful login clears that username's and address's failures. The
counts live in memory (one guard per AppState), so a restart clears them -
acceptable for a single-process app (see app/state.py).

Trade-off: someone who knows a username can lock its owner out for 15
minutes by failing on purpose. That is the usual price of a lockout - keep
a second admin account, or wait for the lockout to end.
"""

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

USERNAME_MAX_FAILURES = 5
ADDRESS_MAX_FAILURES = 20
WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60


class LoginGuard:
    def __init__(self, username_limit: int = USERNAME_MAX_FAILURES, address_limit: int = ADDRESS_MAX_FAILURES,
                 window_seconds: float = WINDOW_SECONDS, lockout_seconds: float = LOCKOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        """`clock` is injectable so tests can move time forward without waiting."""
        self._limits = {"user": username_limit, "addr": address_limit}
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._failures: Dict[Tuple[str, str], Deque[float]] = {}
        self._locked_until: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _keys(username: str, address: Optional[str]):
        keys = [("user", (username or "").strip().lower())]
        if address:
            keys.append(("addr", address))
        return keys

    def retry_after(self, username: str, address: Optional[str]) -> int:
        """Seconds until this username/address may try again (0 = not locked)."""
        now = self._clock()
        with self._lock:
            waits = [until - now for key in self._keys(username, address)
                     if (until := self._locked_until.get(key)) is not None and until > now]
        return int(max(waits) + 0.999) if waits else 0

    def record_failure(self, username: str, address: Optional[str]) -> None:
        """Count one failed login; lock the username/address that reached its limit."""
        now = self._clock()
        with self._lock:
            for key in self._keys(username, address):
                times = self._failures.setdefault(key, deque())
                times.append(now)
                while times and times[0] <= now - self.window_seconds:
                    times.popleft()
                if len(times) >= self._limits[key[0]]:
                    self._locked_until[key] = now + self.lockout_seconds
                    times.clear()

    def record_success(self, username: str, address: Optional[str]) -> None:
        """A successful login clears this username's and address's failures."""
        with self._lock:
            for key in self._keys(username, address):
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)

    def __repr__(self) -> str:
        return f"LoginGuard(locked={len(self._locked_until)}, tracked={len(self._failures)})"

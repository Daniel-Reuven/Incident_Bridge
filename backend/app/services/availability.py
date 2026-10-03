"""
Availability checking for org sites (Sites & Mailing Lists subsystem).

    AvailabilityChecker   abstract interface: check(url) -> AvailabilityCheck
    ├── HttpChecker       the real one: one HTTP GET with a short timeout
    └── FakeChecker       scripted results, for tests and the offline demo

Code that needs a check (SiteDirectory.check_site() in app/sites.py) only
ever talks to the AvailabilityChecker interface, so swapping the real HTTP
check for a fake one - or, later, for a third-party uptime service - needs
no change anywhere else. That is the whole point of the abstract class:
every implementation is used through the shared type, never by checking
which one it is.

Classification (classify_response(), shared by every checker):
    no response at all (timeout, DNS failure, refused)   -> DOWN
    HTTP 5xx                                             -> DOWN
    HTTP 4xx, or anything else unusual (1xx)             -> DEGRADED
    HTTP 2xx/3xx, but slower than the "slow" threshold   -> DEGRADED
    HTTP 2xx/3xx in time                                 -> OPERATIONAL
A 4xx means the server is up and answering but refusing this request (e.g.
403 or 404), so the site is reachable yet not healthy - Degraded, not Down.

What a single check observes is not the final word: Site.record_check()
(app/models/site.py) only marks a site Down after FAILURES_BEFORE_DOWN
failed checks in a row.
"""

import os
import time
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import httpx

from app.models import AvailabilityCheck, SiteStatus

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_SLOW_AFTER_MS = 2000.0
_USER_AGENT = "IncidentBridge-AvailabilityCheck/1.0"


def classify_response(http_status: Optional[int], response_ms: Optional[float],
                      slow_after_ms: float = DEFAULT_SLOW_AFTER_MS) -> SiteStatus:
    """
    Map one check's raw outcome to the status it observed - see the module
    docstring for the table. `http_status` is None when no response arrived.
    A pure function (no I/O), so every rule is easy to test on its own.
    """
    if http_status is None or http_status >= 500:
        return SiteStatus.DOWN
    if not 200 <= http_status < 400:
        return SiteStatus.DEGRADED
    if response_ms is not None and response_ms > slow_after_ms:
        return SiteStatus.DEGRADED
    return SiteStatus.OPERATIONAL


class AvailabilityChecker(ABC):
    """
    Interface every availability checker implements. check() must never
    raise for an unreachable site - "unreachable" is a normal result
    (observed_status DOWN with an `error`), not an exception. Exceptions are
    reserved for genuine programming errors.
    """

    @abstractmethod
    def check(self, url: str) -> AvailabilityCheck:
        """Check `url` once and return what was observed."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


class HttpChecker(AvailabilityChecker):
    """
    Checks a URL with a single HTTP GET.

    Only the response HEADERS are awaited (the body is never downloaded), and
    response_ms is the time until they arrived. Redirects are NOT followed: a
    3xx already proves the server is up and answering. `transport` exists
    only so tests can plug in httpx.MockTransport instead of the network.

    Configuration (see from_env()):
        SITE_CHECK_TIMEOUT_SECONDS  default 5    - give up after this long
        SITE_CHECK_SLOW_MS          default 2000 - slower than this = Degraded
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 slow_after_ms: float = DEFAULT_SLOW_AFTER_MS,
                 transport: Optional[httpx.BaseTransport] = None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if slow_after_ms <= 0:
            raise ValueError("slow_after_ms must be greater than 0")
        self.timeout_seconds = timeout_seconds
        self.slow_after_ms = slow_after_ms
        self._transport = transport

    @classmethod
    def from_env(cls) -> "HttpChecker":
        """
        Build a checker from SITE_CHECK_TIMEOUT_SECONDS / SITE_CHECK_SLOW_MS,
        falling back to the defaults when unset. Raises ValueError with a
        clear message if a value is set but is not a positive number.
        """
        def read(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number, got {raw!r}") from None
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0, got {raw!r}")
            return value

        return cls(timeout_seconds=read("SITE_CHECK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
                   slow_after_ms=read("SITE_CHECK_SLOW_MS", DEFAULT_SLOW_AFTER_MS))

    def check(self, url: str) -> AvailabilityCheck:
        """One GET request; every network failure becomes a DOWN result, never an exception."""
        checked_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False,
                              headers={"User-Agent": _USER_AGENT}, transport=self._transport) as client:
                with client.stream("GET", url) as response:
                    response_ms = (time.perf_counter() - started) * 1000
                    status_code = response.status_code
        except httpx.TimeoutException:
            return AvailabilityCheck(checked_at, SiteStatus.DOWN,
                                     error=f"timed out after {self.timeout_seconds:g}s")
        except httpx.ConnectError as exc:
            # Covers DNS failures ("name not known") and refused connections.
            return AvailabilityCheck(checked_at, SiteStatus.DOWN, error=f"connection failed ({exc})")
        except httpx.HTTPError as exc:
            return AvailabilityCheck(checked_at, SiteStatus.DOWN, error=f"request failed ({exc.__class__.__name__})")
        observed = classify_response(status_code, response_ms, self.slow_after_ms)
        return AvailabilityCheck(checked_at, observed, http_status=status_code, response_ms=round(response_ms, 1))

    def __repr__(self) -> str:
        return f"HttpChecker(timeout_seconds={self.timeout_seconds:g}, slow_after_ms={self.slow_after_ms:g})"


class FakeChecker(AvailabilityChecker):
    """
    Returns scripted results instead of touching the network - for unit
    tests and the offline demo (main.py).

    `script` maps a URL to the results to return for it, in order. Each
    result is either an HTTP status code (int, answered in 100 ms) or None
    for "no response". Once a URL's script runs out, its LAST result keeps
    repeating. A URL that is not in the script always answers HTTP 200.
    Every checked URL is appended to `calls`, so tests can assert what was
    (and was not) checked.

    Example:
        FakeChecker({"https://a.example.com": [200, None, None, 200]})
        -> Operational, then no response twice, then back to 200.
    """

    def __init__(self, script: Optional[Dict[str, Iterable[Optional[int]]]] = None,
                 slow_after_ms: float = DEFAULT_SLOW_AFTER_MS):
        self._script: Dict[str, deque] = {url: deque(results) for url, results in (script or {}).items()}
        self._last: Dict[str, Optional[int]] = {}
        self.slow_after_ms = slow_after_ms
        self.calls: List[str] = []

    def check(self, url: str) -> AvailabilityCheck:
        self.calls.append(url)
        queue = self._script.get(url)
        if queue:
            self._last[url] = queue.popleft()
        http_status = self._last.get(url, 200)
        checked_at = datetime.now(timezone.utc)
        if http_status is None:
            return AvailabilityCheck(checked_at, SiteStatus.DOWN, error="no response (scripted)")
        return AvailabilityCheck(checked_at, classify_response(http_status, 100.0, self.slow_after_ms),
                                 http_status=http_status, response_ms=100.0)

    def __repr__(self) -> str:
        return f"FakeChecker(urls={sorted(self._script)!r}, calls={len(self.calls)})"

"""
ApiClient: the project's own small HTTP/JSON client for external services,
built on the `requests` package (Stage 2, section 2). Used today by the AI
providers (app/ai/providers/) instead of each company's official SDK.

It handles the COMMUNICATION only - sending a request and checking that the
reply is usable JSON. What the data means, and what the app does with it, is
decided by the caller (a provider turns a reply into an answer, the worker
posts it on the fault).

Every failure becomes one of four exception types, so callers can react to
each kind differently:

    ApiConnectionError   no reply at all: DNS failure, refused connection...
    └── ApiTimeout       ... or the service did not answer within the timeout
    ApiHTTPError         a reply with an error status (4xx/5xx), found by
                         response.raise_for_status(); has .status_code
    ApiInvalidJSON       a 2xx reply whose body is not JSON
    ApiInvalidData       JSON, but not the structure we need (not an object,
                         a required field missing or of the wrong type -
                         see require())

Retries (get_json only): a GET is safe to repeat, so a timeout or a
connection error is retried - up to `max_attempts` (default 3) attempts in
total, waiting `retry_wait_seconds` x the attempt number in between (1s,
then 2s). After the last attempt the error is raised, with the number of
attempts in its message. An HTTP error (e.g. 401 bad key, 404) or invalid
data is NEVER retried - asking again would get the same answer. post_json()
never retries: a POST (an AI analysis) is not guaranteed to be safe to
repeat, and its time budget is the whole timeout.

Dependencies come in through the constructor - the requests.Session that
sends the requests and the sleep function used between retries - so tests
replace them with fakes and run without a network and without real waits.

Logging: every request, retry and failure is logged WITHOUT headers, query
values or bodies, so API keys and incident data never reach the console
(keys are always sent in headers, never in the URL).

Usage:
    client = ApiClient("https://api.example.com/v1", headers={"x-api-key": key}, timeout_seconds=15)
    data = client.get_json("/models", params={"pageSize": 100})
    reply = client.post_json("/messages", {"model": "m", ...})
    require(reply, "content", list)   # ApiInvalidData if missing or not a list
"""

import logging
import time
from typing import Any, Callable, Dict, Mapping, Optional

import requests

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 1.0


class ApiError(Exception):
    """Base class of every ApiClient failure. The message is short and readable (no secrets)."""


class ApiConnectionError(ApiError):
    """No reply arrived (DNS failure, refused or dropped connection)."""


class ApiTimeout(ApiConnectionError):
    """The service did not answer within the timeout."""


class ApiHTTPError(ApiError):
    """The service answered with an error status (4xx/5xx)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class ApiInvalidJSON(ApiError):
    """A successful reply whose body is not valid JSON."""


class ApiInvalidData(ApiError):
    """Valid JSON that does not have the structure the caller needs."""


def require(data: Any, key: str, expected_type: type, where: str = "the reply") -> Any:
    """
    data[key], checked to be of `expected_type` - else ApiInvalidData naming
    the field, e.g. "the reply has no 'content' list". `data` itself must be
    a dict (anything else is also ApiInvalidData).
    """
    if not isinstance(data, dict):
        raise ApiInvalidData(f"{where} is not a JSON object")
    value = data.get(key)
    if not isinstance(value, expected_type) or isinstance(value, bool) and expected_type is not bool:
        raise ApiInvalidData(f"{where} has no '{key}' {expected_type.__name__}")
    return value


def error_message(response: requests.Response) -> str:
    """
    The human message of an error reply. Google, Anthropic and OpenAI all
    send {"error": {"message": "..."}}; anything else falls back to the
    status reason (never the raw body, which could be long or an HTML page).
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = body["error"].get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return response.reason or "no details"


class ApiClient:
    def __init__(self, base_url: str, headers: Optional[Mapping[str, str]] = None, timeout_seconds: float = 15.0,
                 session: Optional[requests.Session] = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS,
                 sleep: Callable[[float], None] = time.sleep, name: str = "the API",
                 logger: Optional[logging.Logger] = None):
        """
        `base_url`: the service's root, e.g. "https://api.anthropic.com/v1" (no trailing slash needed).
        `headers`: sent with every request (API keys go here, never in the URL).
        `timeout_seconds`: per request, unless a call passes its own.
        `session`, `sleep`: injected for tests (default: a new requests.Session, time.sleep).
        `max_attempts`, `retry_wait_seconds`: GET retries (see the module docstring).
        `name`: how the service is called in messages and logs, e.g. "the Gemini API".
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.headers: Dict[str, str] = dict(headers or {})
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_wait_seconds = retry_wait_seconds
        self.name = name
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._log = logger or logging.getLogger(__name__)

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None,
                 timeout_seconds: Optional[float] = None) -> dict:
        """
        GET base_url + path and return the JSON object. Retries a timeout or a
        connection error (see the module docstring); raises the last error
        when every attempt failed. Raises ApiHTTPError / ApiInvalidJSON /
        ApiInvalidData at once.
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._send("GET", path, timeout_seconds, params=params)
            except ApiConnectionError as exc:
                if attempt == self.max_attempts:
                    self._log.warning(f"GET {path} to {self.name} failed after {attempt} attempt(s): {exc}")
                    raise type(exc)(f"{exc} (gave up after {attempt} attempts)") from None
                wait = self.retry_wait_seconds * attempt
                self._log.warning(f"GET {path} to {self.name} failed (attempt {attempt} of {self.max_attempts}): "
                                  f"{exc} - retrying in {wait:g}s")
                self._sleep(wait)
        raise AssertionError("unreachable")     # the loop always returns or raises

    def post_json(self, path: str, body: Mapping[str, Any], timeout_seconds: Optional[float] = None) -> dict:
        """POST `body` as JSON to base_url + path and return the JSON object. One attempt, no retry."""
        return self._send("POST", path, timeout_seconds, json=body)

    def _send(self, method: str, path: str, timeout_seconds: Optional[float], **kwargs) -> dict:
        """One request: send it, check the status, decode the JSON. Raises an ApiError subclass."""
        timeout = timeout_seconds or self.timeout_seconds
        self._log.debug(f"{method} {path} to {self.name} (timeout {timeout:g}s)")
        try:
            response = self._session.request(method, self.base_url + path, headers=self.headers,
                                             timeout=timeout, **kwargs)
        except requests.Timeout:                # before ConnectionError: ConnectTimeout is both
            raise ApiTimeout(f"{self.name} did not answer within {timeout:g} seconds") from None
        except requests.ConnectionError as exc:
            raise ApiConnectionError(f"could not connect to {self.name} ({type(exc).__name__})") from None
        except requests.RequestException as exc:
            raise ApiConnectionError(f"the request to {self.name} failed ({type(exc).__name__})") from None

        try:
            response.raise_for_status()
        except requests.HTTPError:
            raise ApiHTTPError(response.status_code, error_message(response)) from None

        try:
            data = response.json()
        except ValueError:
            raise ApiInvalidJSON(f"{self.name} returned a reply that is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiInvalidData(f"{self.name} returned JSON that is not an object")
        return data

    def __repr__(self) -> str:
        return (f"ApiClient({self.base_url!r}, timeout_seconds={self.timeout_seconds:g}, "
                f"max_attempts={self.max_attempts})")

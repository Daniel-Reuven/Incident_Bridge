"""
Unit tests for app/services/availability.py: classify_response(),
HttpChecker (against httpx.MockTransport - no real network) and FakeChecker.

Run from backend/:  python -m pytest tests/test_services/test_availability.py -v
"""

import httpx
import pytest

from app.models import AvailabilityCheck, SiteStatus
from app.services import AvailabilityChecker, FakeChecker, HttpChecker, classify_response


# --- classify_response ---------------------------------------------------

@pytest.mark.parametrize("http_status, response_ms, expected", [
    (200, 100, SiteStatus.OPERATIONAL),
    (204, 1999, SiteStatus.OPERATIONAL),
    (301, 50, SiteStatus.OPERATIONAL),       # a redirect proves the server answers
    (200, 2500, SiteStatus.DEGRADED),        # slow
    (404, 50, SiteStatus.DEGRADED),          # up, but refusing
    (403, 50, SiteStatus.DEGRADED),
    (101, 50, SiteStatus.DEGRADED),          # unusual
    (500, 50, SiteStatus.DOWN),
    (503, 50, SiteStatus.DOWN),
    (None, None, SiteStatus.DOWN),           # no response at all
])
def test_classify_response(http_status, response_ms, expected):
    assert classify_response(http_status, response_ms, slow_after_ms=2000) == expected


def test_the_abstract_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        AvailabilityChecker()


# --- HttpChecker -----------------------------------------------------------

def checker_returning(handler, **kwargs) -> HttpChecker:
    return HttpChecker(transport=httpx.MockTransport(handler), **kwargs)


def test_http_200_is_operational():
    seen = {}

    def handler(request):
        seen["method"], seen["agent"] = request.method, request.headers["user-agent"]
        return httpx.Response(200)

    check = checker_returning(handler).check("https://example.com")
    assert isinstance(check, AvailabilityCheck)
    assert (check.observed_status, check.http_status, check.error) == (SiteStatus.OPERATIONAL, 200, None)
    assert check.response_ms is not None and check.response_ms >= 0
    assert seen == {"method": "GET", "agent": "IncidentBridge-AvailabilityCheck/1.0"}


def test_redirects_are_not_followed():
    def handler(request):
        assert request.url.path == "/"   # would fail on a followed redirect to /elsewhere
        return httpx.Response(302, headers={"location": "https://example.com/elsewhere"})

    check = checker_returning(handler).check("https://example.com/")
    assert (check.http_status, check.observed_status) == (302, SiteStatus.OPERATIONAL)


def test_server_errors_and_client_errors():
    assert checker_returning(lambda r: httpx.Response(503)).check("https://x.example").observed_status \
        == SiteStatus.DOWN
    assert checker_returning(lambda r: httpx.Response(404)).check("https://x.example").observed_status \
        == SiteStatus.DEGRADED


@pytest.mark.parametrize("exception, expected_error", [
    (httpx.ConnectTimeout("slow"), "timed out after 3s"),
    (httpx.ReadTimeout("slow"), "timed out after 3s"),
    (httpx.ConnectError("[Errno -2] Name or service not known"), "connection failed"),
    (httpx.RemoteProtocolError("garbage"), "request failed (RemoteProtocolError)"),
])
def test_network_failures_become_down_results_not_exceptions(exception, expected_error):
    def handler(request):
        raise exception

    check = checker_returning(handler, timeout_seconds=3).check("https://x.example")
    assert check.observed_status == SiteStatus.DOWN
    assert check.http_status is None and check.response_ms is None
    assert expected_error in check.error
    assert check.summary.startswith("no response: ")


def test_constructor_validation():
    with pytest.raises(ValueError, match="timeout_seconds"):
        HttpChecker(timeout_seconds=0)
    with pytest.raises(ValueError, match="slow_after_ms"):
        HttpChecker(slow_after_ms=-1)


def test_from_env_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("SITE_CHECK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SITE_CHECK_SLOW_MS", raising=False)
    default = HttpChecker.from_env()
    assert (default.timeout_seconds, default.slow_after_ms) == (5.0, 2000.0)
    monkeypatch.setenv("SITE_CHECK_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("SITE_CHECK_SLOW_MS", "800")
    custom = HttpChecker.from_env()
    assert (custom.timeout_seconds, custom.slow_after_ms) == (2.5, 800.0)
    assert repr(custom) == "HttpChecker(timeout_seconds=2.5, slow_after_ms=800)"


@pytest.mark.parametrize("value, message", [("fast", "must be a number"), ("0", "must be greater than 0"),
                                            ("-3", "must be greater than 0")])
def test_from_env_rejects_bad_values(monkeypatch, value, message):
    monkeypatch.setenv("SITE_CHECK_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match=f"SITE_CHECK_TIMEOUT_SECONDS {message}"):
        HttpChecker.from_env()


# --- FakeChecker --------------------------------------------------------

def test_fake_checker_follows_its_script_then_repeats_the_last_result():
    fake = FakeChecker({"https://a.example": [200, None, 503]})
    observed = [fake.check("https://a.example").observed_status for _ in range(4)]
    assert observed == [SiteStatus.OPERATIONAL, SiteStatus.DOWN, SiteStatus.DOWN, SiteStatus.DOWN]
    assert fake.check("https://a.example").http_status == 503


def test_fake_checker_unknown_url_answers_200_and_calls_are_recorded():
    fake = FakeChecker()
    assert fake.check("https://other.example").http_status == 200
    assert fake.calls == ["https://other.example"]


def test_fake_checker_no_response_result():
    check = FakeChecker({"https://a.example": [None]}).check("https://a.example")
    assert check.error == "no response (scripted)" and check.http_status is None


def test_summary_text():
    from datetime import datetime, timezone
    check = AvailabilityCheck(datetime.now(timezone.utc), SiteStatus.OPERATIONAL, http_status=200, response_ms=123.4)
    assert check.summary == "HTTP 200 in 123 ms"

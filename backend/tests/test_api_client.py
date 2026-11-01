"""
Tests for app/api_client.py - the project's own HTTP/JSON client (requests).

No network and no real waiting: the requests.Session is a Mock whose
request() returns prepared requests.Response objects or raises requests'
own exceptions, and the sleep between retries is a Mock too. Only the
EXTERNAL dependency (the session) is replaced - the ApiClient code under
test (status check, JSON decoding, validation, retries) runs for real.

Run from backend/:  python -m pytest tests/test_api_client.py -v
"""

import json
import logging
from unittest.mock import Mock

import pytest
import requests

from app.api_client import (ApiClient, ApiConnectionError, ApiHTTPError, ApiInvalidData, ApiInvalidJSON, ApiTimeout,
                            require)


def response(status=200, body=None, text=None, reason="OK"):
    """A real requests.Response holding `body` as JSON (or raw `text`)."""
    resp = requests.Response()
    resp.status_code = status
    resp.reason = reason
    resp._content = (text if text is not None else json.dumps(body if body is not None else {})).encode()
    resp.url = "https://api.example.com/v1/x"
    return resp


@pytest.fixture
def session():
    return Mock(spec=requests.Session)


@pytest.fixture
def sleep():
    return Mock()


@pytest.fixture
def client(session, sleep):
    return ApiClient("https://api.example.com/v1/", headers={"x-api-key": "SECRET-KEY"}, timeout_seconds=7,
                     session=session, sleep=sleep, name="the Example API")


# --- success ---------------------------------------------------------------------

def test_get_sends_params_headers_and_timeout_and_returns_the_object(client, session):
    session.request.return_value = response(body={"models": []})
    assert client.get_json("/models", params={"pageSize": 100}) == {"models": []}
    session.request.assert_called_once_with("GET", "https://api.example.com/v1/models",
                                            headers={"x-api-key": "SECRET-KEY"}, timeout=7,
                                            params={"pageSize": 100})


def test_post_sends_the_body_as_json(client, session):
    session.request.return_value = response(body={"content": []})
    assert client.post_json("/messages", {"model": "m"}, timeout_seconds=30) == {"content": []}
    session.request.assert_called_once_with("POST", "https://api.example.com/v1/messages",
                                            headers={"x-api-key": "SECRET-KEY"}, timeout=30, json={"model": "m"})


# --- each kind of failure ----------------------------------------------------------

@pytest.mark.parametrize("status, body, message", [
    (401, {"error": {"message": "invalid x-api-key"}}, "HTTP 401: invalid x-api-key"),
    (404, {"error": {"message": "model not found"}}, "HTTP 404: model not found"),
    (500, None, "HTTP 500: Internal Server Error"),          # no error JSON -> the reason phrase
])
def test_an_error_status_raises_api_http_error_with_the_services_message(client, session, status, body, message):
    session.request.return_value = response(status, text="<html>oops</html>" if body is None else json.dumps(body),
                                            reason="Internal Server Error")
    with pytest.raises(ApiHTTPError) as caught:
        client.post_json("/x", {})
    assert caught.value.status_code == status and str(caught.value) == message


def test_a_reply_that_is_not_json_raises_api_invalid_json(client, session):
    session.request.return_value = response(text="<html>proxy page</html>")
    with pytest.raises(ApiInvalidJSON, match="not valid JSON"):
        client.get_json("/x")


def test_json_that_is_not_an_object_raises_api_invalid_data(client, session):
    session.request.return_value = response(text="[1, 2]")
    with pytest.raises(ApiInvalidData, match="not an object"):
        client.get_json("/x")


def test_a_timeout_on_post_raises_api_timeout_at_once(client, session, sleep):
    session.request.side_effect = requests.ReadTimeout("slow")
    with pytest.raises(ApiTimeout, match="did not answer within 7 seconds"):
        client.post_json("/x", {})
    assert session.request.call_count == 1 and not sleep.called       # POST is never retried


# --- GET retries -------------------------------------------------------------------

@pytest.mark.parametrize("transient", [requests.ConnectTimeout("t"), requests.ReadTimeout("t"),
                                       requests.ConnectionError("refused")])
def test_get_succeeds_after_a_temporary_failure(client, session, sleep, transient):
    session.request.side_effect = [transient, response(body={"ok": True})]
    assert client.get_json("/x") == {"ok": True}
    assert session.request.call_count == 2
    sleep.assert_called_once_with(1.0)                                # waited before the 2nd attempt


def test_get_gives_up_after_three_attempts_with_a_clear_error(client, session, sleep):
    session.request.side_effect = requests.ConnectionError("network down")
    with pytest.raises(ApiConnectionError, match=r"could not connect to the Example API .*gave up after 3 attempts"):
        client.get_json("/x")
    assert session.request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]   # growing wait, none after the last


def test_get_timeouts_on_every_attempt_raise_api_timeout(client, session):
    session.request.side_effect = requests.ReadTimeout("slow")
    with pytest.raises(ApiTimeout, match="gave up after 3 attempts"):
        client.get_json("/x")


@pytest.mark.parametrize("reply, error", [
    (response(401, {"error": {"message": "bad key"}}), ApiHTTPError),    # authorization: never retried
    (response(text="not json"), ApiInvalidJSON),                         # invalid data: never retried
])
def test_get_does_not_retry_an_http_error_or_invalid_data(client, session, sleep, reply, error):
    session.request.return_value = reply
    with pytest.raises(error):
        client.get_json("/x")
    assert session.request.call_count == 1 and not sleep.called


def test_logs_never_contain_the_api_key(session, sleep, caplog):
    logger = logging.getLogger("test.api_client")
    client = ApiClient("https://api.example.com", headers={"x-api-key": "SECRET-KEY"}, session=session,
                       sleep=sleep, logger=logger)
    session.request.side_effect = requests.ConnectionError("down")
    caplog.set_level(logging.DEBUG, logger="test.api_client")
    with pytest.raises(ApiConnectionError):
        client.get_json("/x", params={"q": "value"})
    assert caplog.records and "SECRET-KEY" not in caplog.text


# --- require(): field validation ------------------------------------------------

def test_require_returns_a_field_of_the_right_type():
    assert require({"content": [1]}, "content", list) == [1]


@pytest.mark.parametrize("data, message", [
    ({}, "the reply has no 'content' list"),                         # missing
    ({"content": "text"}, "the reply has no 'content' list"),        # wrong type
    (["content"], "the reply is not a JSON object"),                 # not an object
])
def test_require_rejects_a_missing_or_wrong_field(data, message):
    with pytest.raises(ApiInvalidData, match=message):
        require(data, "content", list)


@pytest.mark.parametrize("kwargs", [{"timeout_seconds": 0}, {"max_attempts": 0}])
def test_invalid_client_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ApiClient("https://api.example.com", **kwargs)

"""
Tests for the three real providers (app/ai/providers/gemini_provider.py,
claude_provider.py, openai_provider.py) WITHOUT a real API: each provider's
own REST code (HttpProvider + app/api_client.py, over `requests`) talks to a
small local HTTP server that answers like the real API, returns an error,
returns a broken reply, or never answers (to trigger the timeout) - per
model, so the fallback from a busy model to the next one is tested end to
end too. The stub also records the request headers, so the tests check
that each key is sent the way its API expects (and never in the URL).

Run from backend/:  python -m pytest tests/test_ai/test_providers.py -v
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.ai.providers.base import AnalysisError, AnalysisResult

REPLIES = {
    "generateContent": {"candidates": [{"content": {"parts": [{"text": "Gemini answer"}], "role": "model"},
                                        "finishReason": "STOP"}]},
    "/messages": {"id": "m", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "Claude answer"}], "stop_sequence": None,
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
    "/responses": {"id": "r", "object": "response", "created_at": 1, "model": "m", "status": "completed",
                   "output": [{"type": "message", "id": "o", "status": "completed", "role": "assistant",
                               "content": [{"type": "output_text", "text": "OpenAI answer", "annotations": []}]}],
                   "parallel_tool_calls": False, "tool_choice": "auto", "tools": []},
}

# A well-formed reply that holds no text (e.g. Gemini blocked the prompt).
EMPTY_REPLIES = {
    "generateContent": {"candidates": [{"finishReason": "SAFETY"}]},
    "/messages": {"content": []},
    "/responses": {"output": [{"type": "reasoning", "summary": []}]},
}


class StubApi:
    """
    A local HTTP server. `mode`: "ok" (a valid reply), "401" (bad key), "429" (out of quota),
    "503" (overloaded) or "hang" (never replies in time). `modes` overrides it per model.
    `asked` lists the model of every request, in order.
    """

    def __init__(self):
        self.mode = "ok"
        self.modes = {}
        self.listing = []
        self.requests = []
        self.headers = []
        self.paths = []
        self.asked = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                """The model listing (Gemini's GET .../models), answered from stub.listing."""
                stub.paths.append(self.path)
                data = json.dumps({"models": stub.listing}).encode()
                self.send_response(200 if stub.mode != "503" else 503)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["content-length"])))
                stub.requests.append(request)
                stub.headers.append(dict(self.headers))
                stub.paths.append(self.path)
                # Gemini puts the model in the path (.../models/<model>:generateContent), the others in the body.
                model = request.get("model") or self.path.split("/models/")[-1].split(":")[0]
                stub.asked.append(model)
                mode = stub.modes.get(model, stub.mode)
                if mode == "hang":
                    time.sleep(2)
                    return
                if mode in ("401", "429", "503"):
                    status = int(mode)
                    data = json.dumps({"error": {"code": status, "message": f"error {status}", "type": "api_error"}})
                elif mode == "not-json":
                    status, data = 200, "<html>proxy error page</html>"
                elif mode == "wrong-shape":
                    status, data = 200, json.dumps({"unexpected": True})
                elif mode == "empty":
                    status, data = 200, json.dumps(EMPTY_REPLIES[next(m for m in EMPTY_REPLIES if m in self.path)])
                else:
                    status = 200
                    data = json.dumps(next(reply for marker, reply in REPLIES.items() if marker in self.path))
                data = data.encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub():
    api = StubApi()
    yield api
    api.server.shutdown()


def gemini(url, timeout, models=("m",), **settings):
    from app.ai.providers.gemini_provider import GeminiProvider
    return GeminiProvider(models=models, api_key="gemini-key", timeout_seconds=timeout, base_url=url + "/v1beta",
                          **settings)


def claude(url, timeout, models=("m",)):
    from app.ai.providers.claude_provider import ClaudeProvider
    return ClaudeProvider(models=models, api_key="claude-key", timeout_seconds=timeout, base_url=url + "/v1")


def openai_(url, timeout, models=("m",)):
    from app.ai.providers.openai_provider import OpenAIProvider
    return OpenAIProvider(models=models, api_key="openai-key", timeout_seconds=timeout, base_url=url + "/v1")


PROVIDERS = [(gemini, "Gemini answer"), (claude, "Claude answer"), (openai_, "OpenAI answer")]


@pytest.mark.parametrize("make, expected", PROVIDERS)
def test_a_successful_call_returns_the_text_and_sends_both_parts(stub, make, expected):
    assert make(stub.url, 5).analyze("THE INSTRUCTIONS", '{"incident": "DATA"}') == AnalysisResult(expected, "m")
    sent = json.dumps(stub.requests[0])
    assert "THE INSTRUCTIONS" in sent and "DATA" in sent


@pytest.mark.parametrize("make, _", PROVIDERS)
def test_a_rejected_key_fails_at_once_without_trying_other_models(stub, make, _):
    stub.mode = "401"
    with pytest.raises(AnalysisError, match="returned an error \\(401\\)"):
        make(stub.url, 5, models=("m", "backup")).analyze("i", "{}")
    assert stub.asked == ["m"]                          # no retry, no fallback


@pytest.mark.parametrize("make, expected", PROVIDERS)
@pytest.mark.parametrize("busy", ["503", "429"])
def test_a_busy_model_falls_back_to_the_next_one(stub, make, expected, busy):
    stub.modes = {"best": busy}
    result = make(stub.url, 5, models=("best", "m")).analyze("i", "{}")
    assert result == AnalysisResult(expected, "m")      # the model that actually answered
    assert stub.asked == ["best", "m"]


@pytest.mark.parametrize("make, _", PROVIDERS)
def test_when_every_model_is_busy_the_error_names_each_one(stub, make, _):
    stub.modes = {"a": "503", "b": "429"}
    with pytest.raises(AnalysisError) as caught:
        make(stub.url, 5, models=("a", "b")).analyze("i", "{}")
    assert str(caught.value) == ("all 2 models were unavailable - a is overloaded (503); "
                                 "b is rate limited or out of quota (429)")


@pytest.mark.parametrize("make, _", PROVIDERS)
def test_a_slow_api_times_out_without_retrying(stub, make, _):
    stub.mode = "hang"
    started = time.monotonic()
    with pytest.raises(AnalysisError, match="timed out after 0.5 seconds"):
        make(stub.url, 0.5, models=("m", "backup")).analyze("i", "{}")
    assert time.monotonic() - started < 1.8
    assert stub.asked == ["m"]                          # a timeout does not fall back


@pytest.mark.parametrize("make, _", PROVIDERS)
@pytest.mark.parametrize("mode, message", [
    ("not-json", "unexpected reply: .* not valid JSON"),
    ("wrong-shape", "unexpected reply: the reply has no '(candidates|content|output)' list"),
    ("empty", "returned an empty answer"),
])
def test_a_broken_or_empty_reply_fails_without_trying_other_models(stub, make, _, mode, message):
    stub.mode = mode
    with pytest.raises(AnalysisError, match=message):
        make(stub.url, 5, models=("m", "backup")).analyze("i", "{}")
    assert stub.asked == ["m"]                          # another model would not fix a broken reply


def test_an_unreachable_api_fails_with_a_clear_message():
    """
    A refused connection, simulated with a mocked requests.Session: connecting
    to a closed local port is refused at once on Linux but only times out on
    Windows, so a real socket would make this test depend on the OS.
    """
    from unittest.mock import Mock
    import requests
    from app.ai.providers.claude_provider import ClaudeProvider
    from app.api_client import ApiClient
    session = Mock(spec=requests.Session)
    session.request.side_effect = requests.ConnectionError("connection refused")
    client = ApiClient("http://api.invalid/v1", timeout_seconds=2, session=session, name="the Claude API")
    provider = ClaudeProvider(models=("m",), api_key="k", timeout_seconds=2, client=client)
    with pytest.raises(AnalysisError, match="could not reach the Claude API"):
        provider.analyze("i", "{}")


@pytest.mark.parametrize("make, header, value", [
    (gemini, "x-goog-api-key", "gemini-key"),
    (claude, "x-api-key", "claude-key"),
    (openai_, "Authorization", "Bearer openai-key"),
])
def test_the_key_is_sent_in_its_header_and_never_in_the_url(stub, make, header, value):
    make(stub.url, 5).analyze("i", "{}")
    sent = {name.lower(): text for name, text in stub.headers[0].items()}
    assert sent[header.lower()] == value
    assert "key" not in stub.paths[0]


@pytest.mark.parametrize("make, path", [
    (gemini, "/v1beta/models/m:generateContent"), (claude, "/v1/messages"), (openai_, "/v1/responses"),
])
def test_each_provider_posts_to_its_api_path(stub, make, path):
    make(stub.url, 5).analyze("i", "{}")
    assert stub.paths == [path]


def test_claude_sends_the_api_version_and_a_token_limit(stub):
    claude(stub.url, 5).analyze("THE INSTRUCTIONS", "{}")
    assert {k.lower(): v for k, v in stub.headers[0].items()}["anthropic-version"] == "2023-06-01"
    assert stub.requests[0]["system"] == "THE INSTRUCTIONS" and stub.requests[0]["max_tokens"] > 0


# --- Gemini model discovery through the REST listing ---------------------------------

def listed(name, *actions):
    return {"name": f"models/{name}", "supportedGenerationMethods": list(actions)}


def test_gemini_discovery_reads_the_listing_and_updates_the_list(stub):
    stub.listing = [listed("gemini-3.9-flash", "generateContent"), listed("gemini-3.8-flash", "generateContent"),
                    listed("gemini-4-pro", "generateContent"), listed("text-embedding-005", "embedContent")]
    provider = gemini(stub.url, 5, models=("gemini-3.8-flash", "gemini-3.1-flash-lite"),
                      tier="free", discovery=True, extend=True)
    assert provider.available_models() == ["gemini-3.9-flash", "gemini-3.8-flash", "gemini-4-pro"]
    summary = provider.refresh_models()
    assert provider.models == ["gemini-3.9-flash", "gemini-3.8-flash"]      # pro not added on the free tier
    assert "added gemini-3.9-flash" in summary and "removed gemini-3.1-flash-lite" in summary


def test_gemini_discovery_off_makes_no_call(stub):
    provider = gemini(stub.url, 5, models=("m",))
    assert provider.refresh_models() is None and stub.asked == []


def test_a_failed_listing_raises_and_leaves_the_list_alone(stub):
    stub.mode = "503"
    provider = gemini(stub.url, 5, models=("m",), discovery=True)
    with pytest.raises(Exception):
        provider.refresh_models()
    assert provider.models == ["m"]


def test_the_listing_sends_the_page_size_and_follows_page_tokens():
    """Pagination with a mocked ApiClient: every page is read, in order, and only generateContent models kept."""
    from unittest.mock import Mock
    from app.ai.providers.gemini_provider import GeminiProvider
    client = Mock()
    client.get_json.side_effect = [
        {"models": [listed("gemini-a", "generateContent")], "nextPageToken": "page2"},
        {"models": [listed("embedder", "embedContent"), listed("gemini-b", "generateContent")]},
    ]
    provider = GeminiProvider(models=("m",), api_key="k", timeout_seconds=5, client=client)
    assert provider.available_models() == ["gemini-a", "gemini-b"]
    first, second = (call.kwargs["params"] for call in client.get_json.call_args_list)
    assert first == {"pageSize": 100} and second == {"pageSize": 100, "pageToken": "page2"}

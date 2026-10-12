"""
Tests for the three real providers (app/ai/providers/gemini_provider.py,
claude_provider.py, openai_provider.py) WITHOUT a real API: each provider's
official SDK talks to a small local HTTP server that answers like the real
API, returns an error, or never answers (to trigger the timeout) - per
model, so the fallback from a busy model to the next one is tested through
the real SDKs too.

Each test is skipped if that provider's SDK is not installed
(pip install -e ".[ai]" installs all three).

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


class StubApi:
    """
    A local HTTP server. `mode`: "ok" (a valid reply), "401" (bad key), "429" (out of quota),
    "503" (overloaded) or "hang" (never replies in time). `modes` overrides it per model.
    `asked` lists the model of every request, in order.
    """

    def __init__(self):
        self.mode = "ok"
        self.modes = {}
        self.requests = []
        self.asked = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["content-length"])))
                stub.requests.append(request)
                # Gemini puts the model in the path (.../models/<model>:generateContent), the others in the body.
                model = request.get("model") or self.path.split("/models/")[-1].split(":")[0]
                stub.asked.append(model)
                mode = stub.modes.get(model, stub.mode)
                if mode == "hang":
                    time.sleep(2)
                    return
                if mode in ("401", "429", "503"):
                    status = int(mode)
                    body = {"error": {"code": status, "message": f"error {status}", "type": "api_error"}}
                else:
                    status = 200
                    body = next(reply for marker, reply in REPLIES.items() if marker in self.path)
                data = json.dumps(body).encode()
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


def gemini(url, timeout, models=("m",)):
    pytest.importorskip("google.genai")
    from google import genai
    from google.genai import types
    from app.ai.providers.gemini_provider import GeminiProvider
    provider = GeminiProvider(models=models, api_key="k", timeout_seconds=timeout)
    provider._client = genai.Client(api_key="k", http_options=types.HttpOptions(base_url=url, timeout=int(timeout * 1000)))
    return provider


def claude(url, timeout, models=("m",)):
    anthropic = pytest.importorskip("anthropic")
    from app.ai.providers.claude_provider import ClaudeProvider
    provider = ClaudeProvider(models=models, api_key="k", timeout_seconds=timeout)
    provider._client = anthropic.Anthropic(api_key="k", base_url=url, timeout=timeout, max_retries=0)
    return provider


def openai_(url, timeout, models=("m",)):
    openai = pytest.importorskip("openai")
    from app.ai.providers.openai_provider import OpenAIProvider
    provider = OpenAIProvider(models=models, api_key="k", timeout_seconds=timeout)
    provider._client = openai.OpenAI(api_key="k", base_url=url + "/v1", timeout=timeout, max_retries=0)
    return provider


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

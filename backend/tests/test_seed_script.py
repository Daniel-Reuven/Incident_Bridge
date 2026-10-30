"""
Tests for backend/seed_from_jsonl.py's login (no server, no network): the
HTTP calls go to an httpx.MockTransport that answers like the real
POST /auth/login, and the password prompt is replaced.

Run from backend/:  python -m pytest tests/test_seed_script.py -v
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import seed_from_jsonl  # noqa: E402


def server(passwords, status_for_wrong=401, must_change=False):
    """A fake /auth/login that accepts `passwords`; records every password it was sent."""
    sent = []

    def handle(request):
        import json
        body = json.loads(request.content)
        sent.append(body["password"])
        if body["password"] in passwords:
            return httpx.Response(200, json={"username": body["username"], "must_change_password": must_change})
        return httpx.Response(status_for_wrong, json={"detail": "Too many failed logins - try again in about 15 minute(s)."
        if status_for_wrong == 429 else "Invalid username or password."})

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test"), sent


@pytest.fixture
def typed(monkeypatch):
    """Answers for the password prompt, in order."""
    answers = []
    monkeypatch.setattr(seed_from_jsonl, "_ask_password", lambda username: answers.pop(0))
    return answers


def test_the_env_password_is_used_when_it_still_works(typed):
    client, sent = server({"Start#Pass1"})
    seed_from_jsonl._login(client, "admin", "Start#Pass1", ask_first=False)
    assert sent == ["Start#Pass1"]  # nothing asked


def test_a_changed_password_is_asked_for_once(typed, capsys):
    client, sent = server({"Own#Pass99"})
    typed.append("Own#Pass99")
    seed_from_jsonl._login(client, "admin", "Start#Pass1", ask_first=False)
    assert sent == ["Start#Pass1", "Own#Pass99"]
    assert "no longer works" in capsys.readouterr().out


def test_it_gives_up_after_three_wrong_passwords(typed):
    client, sent = server({"right"})
    typed.extend(["w1", "w2", "w3"])
    with pytest.raises(SystemExit, match="giving up"):
        seed_from_jsonl._login(client, "admin", "old", ask_first=False)
    assert sent == ["old", "w1", "w2", "w3"]


def test_with_a_username_it_asks_first(typed):
    client, sent = server({"Boss#Pass1"})
    typed.append("Boss#Pass1")
    seed_from_jsonl._login(client, "boss", None, ask_first=True)
    assert sent == ["Boss#Pass1"]


def test_a_locked_account_stops_with_the_servers_message(typed):
    client, _ = server(set(), status_for_wrong=429)
    with pytest.raises(SystemExit, match="Too many failed logins"):
        seed_from_jsonl._login(client, "admin", "old", ask_first=False)


def test_an_admin_who_must_change_the_password_is_told_so(typed):
    client, _ = server({"Start#Pass1"}, must_change=True)
    with pytest.raises(SystemExit, match="must change the password first"):
        seed_from_jsonl._login(client, "admin", "Start#Pass1", ask_first=False)


def test_without_a_real_terminal_the_password_is_read_as_visible_input(monkeypatch, capsys):
    """An IDE's Run window: no TTY - getpass would wait on an invisible console, so plain input is used."""
    import io

    class FakeStdin(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", FakeStdin("Typed#Pass1\n"))
    monkeypatch.setattr(seed_from_jsonl.getpass, "getpass", lambda *a, **k: pytest.fail("getpass must not be used"))
    assert seed_from_jsonl._ask_password("admin") == "Typed#Pass1"
    out = capsys.readouterr().out
    assert "WILL BE VISIBLE" in out and "Password for 'admin'" in out


def test_in_a_real_terminal_getpass_hides_the_typing(monkeypatch):
    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stdout", type("Out", (), {"isatty": lambda self: True, "flush": lambda self: None,
                                                        "write": lambda self, s: None})())
    monkeypatch.setattr(seed_from_jsonl.getpass, "getpass", lambda prompt, **kwargs: "Hidden#Pass1")
    assert seed_from_jsonl._ask_password("admin") == "Hidden#Pass1"

"""
Unit tests for app.services.password_policy.PasswordPolicy - the
character-mix rule: 8-128 characters, an uppercase letter, a lowercase
letter, a digit and a symbol; not containing the username; not a common
password. Expected results come from that rule, written out by hand.

Run just this file:
    cd backend
    python -m pytest tests/test_services/test_password_policy.py -v
"""

import pytest

from app.services.password_policy import PasswordPolicy


@pytest.mark.parametrize("password", [
    "Passw0rd!1",          # every class once or more
    "Abc1234!",            # exactly 8 characters
    "Correct Horse 9",     # a space counts as a symbol
    "Ünïcödé#9a",          # non-ASCII letters count as letters
    "A1!" + "a" * 125,     # exactly 128 characters
])
def test_acceptable_passwords_pass(password):
    PasswordPolicy.validate(password)
    assert PasswordPolicy.problems(password) == []


@pytest.mark.parametrize("password, message", [
    ("Ab1!xyz", "at least 8 characters"),
    ("", "at least 8 characters"),
    (None, "at least 8 characters"),
    ("A1!" + "a" * 126, "at most 128 characters"),
    ("passw0rd!x", "uppercase letter"),
    ("PASSW0RD!X", "lowercase letter"),
    ("Password!x", "a digit"),
    ("Passw0rd12", "a symbol"),
    ("P@ssw0rd", "too common"),
    ("Welcome123!", "too common"),
])
def test_each_broken_rule_is_named(password, message):
    with pytest.raises(ValueError, match=message):
        PasswordPolicy.validate(password)


def test_every_problem_is_reported_at_once():
    problems = PasswordPolicy.problems("abc")
    assert len(problems) == 4          # too short, no uppercase, no digit, no symbol


@pytest.mark.parametrize("username", ["tech1", "TECH1", " tech1 "])
def test_the_username_may_not_appear_in_the_password(username):
    with pytest.raises(ValueError, match="must not contain the username"):
        PasswordPolicy.validate("My-Tech1-pass", username)


def test_rules_describe_the_policy_for_the_frontend():
    ids = [rule["id"] for rule in PasswordPolicy.rules()]
    assert ids == ["length", "upper", "lower", "digit", "symbol", "username", "common", "different"]
    assert PasswordPolicy.rules()[0]["text"] == "8 to 128 characters"


def test_dunder_str():
    assert str(PasswordPolicy()) == "PasswordPolicy(8-128 chars, upper + lower + digit + symbol, no username, not common)"


def test_dunder_repr():
    assert repr(PasswordPolicy()) == f"<PasswordPolicy min_length=8 max_length=128 common_passwords={len(PasswordPolicy.COMMON_PASSWORDS)}>"

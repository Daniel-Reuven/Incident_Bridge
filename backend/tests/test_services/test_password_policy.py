"""
Unit tests for app.services.password_policy.PasswordPolicy.

These are "unit" tests in the strictest sense: PasswordPolicy has no
dependencies on anything else in the app (no database, no HTTP, no other
domain objects), so every test here just calls PasswordPolicy.validate()
directly and checks what it does - either nothing (success) or a
ValueError with a specific message (rejection).

Run just this file:
    cd backend
    python -m pytest tests/test_services/test_password_policy.py -v
"""

import pytest

from app.services.password_policy import PasswordPolicy


def test_valid_password_passes():
    """A password meeting every rule should not raise anything at all."""
    PasswordPolicy.validate("Passw0rd1")  # no exception raised = test passes


def test_exactly_minimum_length_passes():
    """8 characters is the documented minimum - it must be ACCEPTED, not rejected."""
    PasswordPolicy.validate("Abcd1234")  # exactly 8 characters


def test_too_short_is_rejected():
    with pytest.raises(ValueError, match="at least 8 characters"):
        PasswordPolicy.validate("Abc123")  # 6 characters


def test_empty_string_is_rejected():
    with pytest.raises(ValueError, match="at least 8 characters"):
        PasswordPolicy.validate("")


def test_none_is_rejected():
    with pytest.raises(ValueError, match="at least 8 characters"):
        PasswordPolicy.validate(None)


def test_spaces_are_rejected():
    with pytest.raises(ValueError, match="letters .* and digits"):
        PasswordPolicy.validate("Pass word1")


def test_symbols_are_rejected():
    with pytest.raises(ValueError, match="letters .* and digits"):
        PasswordPolicy.validate("Passw0rd!")


def test_unicode_letters_are_rejected():
    """Only A-Z/a-z/0-9 are allowed - accented/non-Latin letters must fail even at 8+ characters."""
    with pytest.raises(ValueError):
        PasswordPolicy.validate("Pässw0rd")

def test_dunder_str():
    policy = PasswordPolicy()
    assert str(policy) == "PasswordPolicy(min_length=8, allowed_chars='A-Z, a-z, 0-9')"

def test_dunder_repr():
    policy = PasswordPolicy()
    assert repr(policy) == "<PasswordPolicy min_length=8 pattern='^[A-Za-z0-9]+$'>"
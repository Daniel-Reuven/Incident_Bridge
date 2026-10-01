"""
Unit tests for app.services.severity_scoring.SeverityScorer.

Covers every branch of the current rule-based scoring logic (README
section 2): system_unavailable/security_breach -> CRITICAL,
performance_degraded or 25%+ affected users -> MAJOR, everything else ->
MINOR. If the real scoring rules ever change (or get replaced by an
external service, per the README's stated future plan), update these
tests to match the new rules rather than deleting them.
"""

from app.models.enums import SeverityCategory
from app.services.severity_scoring import SeverityScorer


def test_system_unavailable_is_critical():
    result = SeverityScorer.score({"system_unavailable": True})
    assert result == SeverityCategory.CRITICAL


def test_security_breach_is_critical():
    result = SeverityScorer.score({"security_breach": True})
    assert result == SeverityCategory.CRITICAL


def test_both_critical_flags_together_still_critical():
    """Either flag alone is enough - having both set shouldn't change or break anything."""
    result = SeverityScorer.score({"system_unavailable": True, "security_breach": True})
    assert result == SeverityCategory.CRITICAL


def test_performance_degraded_is_major():
    result = SeverityScorer.score({"performance_degraded": True})
    assert result == SeverityCategory.MAJOR


def test_25_percent_affected_users_is_major():
    """25% is the documented threshold - it must be INCLUSIVE (>=), not exclusive (>)."""
    result = SeverityScorer.score({"affected_users_percent": 25})
    assert result == SeverityCategory.MAJOR


def test_24_percent_affected_users_is_not_major_on_its_own():
    """One point under the threshold must NOT trigger Major by itself."""
    result = SeverityScorer.score({"affected_users_percent": 24})
    assert result == SeverityCategory.MINOR


def test_critical_flag_beats_major_flag():
    """If both a critical-level and a major-level detail are present, Critical wins (it's checked first)."""
    result = SeverityScorer.score({"system_unavailable": True, "performance_degraded": True})
    assert result == SeverityCategory.CRITICAL


def test_cosmetic_only_is_minor():
    result = SeverityScorer.score({"cosmetic_only": True})
    assert result == SeverityCategory.MINOR


def test_empty_details_is_minor():
    result = SeverityScorer.score({})
    assert result == SeverityCategory.MINOR


def test_none_details_is_minor():
    """score() must tolerate details=None (e.g. a fault created with no details dict supplied at all)."""
    result = SeverityScorer.score(None)
    assert result == SeverityCategory.MINOR

def test_dunder_str():
    """Test the string representation of the scorer."""
    scorer = SeverityScorer()
    assert str(scorer) == "SeverityScorer(mode='rule-based')"
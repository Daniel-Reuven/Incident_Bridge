"""Severity scoring service (README section 2)."""

from app.models.enums import SeverityCategory


class SeverityScorer:
    """
    Turns incident details into a SeverityCategory. Deliberately kept as
    its own class, separate from Fault, so this rule-based placeholder can
    later be swapped for a real external scoring service (Stage 2) or a
    more sophisticated rule engine without touching Fault or
    FaultPriorityQueue at all.

    This is an intentionally simple placeholder rule set - refine or
    replace `score()` once the real scoring criteria/sheet is finalized.
    """

    @staticmethod
    def score(details: "dict | None") -> SeverityCategory:
        """
        details is expected to carry (all optional; missing keys default
        to a "not present" / falsy value):
          - system_unavailable: bool
          - security_breach: bool
          - performance_degraded: bool
          - affected_users_percent: float (0-100)
          - cosmetic_only: bool

        Returns exactly one of the three fixed SeverityCategory values -
        there is no other outcome.
        """
        details = details or {}

        if details.get("system_unavailable") or details.get("security_breach"):
            return SeverityCategory.CRITICAL

        if details.get("performance_degraded") or details.get("affected_users_percent", 0) >= 25:
            return SeverityCategory.MAJOR

        return SeverityCategory.MINOR

    def __str__(self) -> str:
        """String representation indicating the current scoring mode."""
        return "SeverityScorer(mode='rule-based')"
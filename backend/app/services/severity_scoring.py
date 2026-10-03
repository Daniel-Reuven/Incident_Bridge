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

    validate_details() is the single place that decides what a valid
    details dict looks like. score() runs it first, so every entry point -
    POST /incidents/faults, the JSONL seed import (Fault.from_dict), and any
    future caller - rejects bad details the same way, with a ValueError
    (HTTP 400 at the API, a skipped record in an import) instead of
    crashing on, e.g., "lots" >= 25.
    """

    # The only keys a details dict may contain, and what each must hold.
    FLAG_KEYS = ("system_unavailable", "security_breach", "performance_degraded", "cosmetic_only")
    PERCENT_KEY = "affected_users_percent"

    @classmethod
    def validate_details(cls, details) -> dict:
        """
        Return `details` (an empty dict for None) if it is valid, otherwise
        raise ValueError explaining what is wrong:
          - it must be a JSON object (dict);
          - only the keys in FLAG_KEYS and PERCENT_KEY are allowed (a typo
            such as "system_unavailble" would otherwise be silently ignored
            and quietly lower the fault's severity);
          - every flag must be true or false;
          - affected_users_percent must be a number from 0 to 100.
        """
        if details is None:
            return {}
        if not isinstance(details, dict):
            raise ValueError(f"'details' must be a JSON object, got {type(details).__name__}")
        allowed = set(cls.FLAG_KEYS) | {cls.PERCENT_KEY}
        unknown = sorted(set(details) - allowed)
        if unknown:
            raise ValueError(f"unknown 'details' field(s): {', '.join(unknown)} "
                             f"(allowed: {', '.join(sorted(allowed))})")
        for key in cls.FLAG_KEYS:
            if key in details and not isinstance(details[key], bool):
                raise ValueError(f"'details.{key}' must be true or false, got {details[key]!r}")
        if cls.PERCENT_KEY in details:
            value = details[cls.PERCENT_KEY]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                raise ValueError(f"'details.{cls.PERCENT_KEY}' must be a number from 0 to 100, got {value!r}")
        return details

    @classmethod
    def score(cls, details: "dict | None") -> SeverityCategory:
        """
        details is expected to carry (all optional; missing keys default
        to a "not present" / falsy value):
          - system_unavailable: bool
          - security_breach: bool
          - performance_degraded: bool
          - affected_users_percent: float (0-100)
          - cosmetic_only: bool

        Returns exactly one of the three fixed SeverityCategory values -
        there is no other outcome. Raises ValueError for invalid details
        (see validate_details()).
        """
        details = cls.validate_details(details)

        if details.get("system_unavailable") or details.get("security_breach"):
            return SeverityCategory.CRITICAL

        if details.get("performance_degraded") or details.get("affected_users_percent", 0) >= 25:
            return SeverityCategory.MAJOR

        return SeverityCategory.MINOR

    def __str__(self) -> str:
        """String representation indicating the current scoring mode."""
        return "SeverityScorer(mode='rule-based')"
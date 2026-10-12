"""
Which Gemini models to try, and in what order - chosen by the key's TIER.

Google offers no API call that says whether a key is on the free tier or
on a paid (billed) tier, so the tier is a setting:

    GEMINI_TIER=free    (default) only models the free tier can use
    GEMINI_TIER=paid    the best paid model first, then the free ones

Each list is ordered best first. The provider tries them in order and falls
back to the next one when a model is busy or out of quota (see base.py).
On a free key, a paid-only model simply answers "no quota", so GEMINI_TIER=paid
with a free key still works - it just wastes one failed call per analysis.

GEMINI_MODELS overrides the tier's list with your own comma-separated list,
e.g. GEMINI_MODELS=gemini-3.6-flash,gemini-3.5-flash-lite - also the way to
avoid spending paid credits on a paid key. The older single GEMINI_MODEL
setting (v0.4.0) still works and is used when GEMINI_MODELS is not set.

The lists follow Google's model and pricing pages as of October 2026
(https://ai.google.dev/gemini-api/docs/models and .../pricing). Models are
retired over time: when one is, it answers "not found" and is skipped like a
busy one, but update these lists (or set GEMINI_MODELS) when that happens.

Model discovery (opt-in, GEMINI_MODEL_DISCOVERY=auto): when it starts (on
the first analysis request), right after creating the provider, the worker thread asks Google which models this key can use (GeminiProvider
.refresh_models()) and refresh_model_list() below updates the list:
  - configured models Google no longer offers are REMOVED (retired);
  - NEWER stable models of the families the tier may use are ADDED to the
    FRONT, newest first: flash and flash-lite on the free tier, plus pro
    when GEMINI_TIER=paid. A model counts as newer when its version number
    is higher than the newest one of its family already in the list.
    Preview / experimental models are never added automatically.
  - a list you set yourself (GEMINI_MODELS, or the older GEMINI_MODEL) is
    only checked - never extended - because it is your explicit choice.
Google's listing does not say which models are free or how good they are,
so "may use" comes from the model family in the name and the tier setting.
No Google API can confirm that a key has paid credits, so Pro models are
added only when GEMINI_TIER=paid says so.

This module imports no SDK, so the settings can be checked without
google-genai installed.
"""

import os
import re
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple

# Free of charge on the free tier, best first.
FREE_TIER_MODELS: Tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)

# A paid key can also use the Pro model; if it is busy, the free models follow.
PAID_TIER_MODELS: Tuple[str, ...] = ("gemini-3.1-pro-preview",) + FREE_TIER_MODELS

TIERS = {"free": FREE_TIER_MODELS, "paid": PAID_TIER_MODELS}
DEFAULT_TIER = "free"


DISCOVERY_MODES = ("off", "auto")
DEFAULT_DISCOVERY = "off"

# Model families each tier may use - the order is also the tie-break when two
# discovered models have the same version (better family first).
TIER_FAMILIES = {"free": ("flash", "flash-lite"), "paid": ("pro", "flash", "flash-lite")}

# A stable model name: "gemini-<version>-<family>", e.g. gemini-3.8-flash.
# Anything with an extra suffix (-preview, -exp, -001, ...) does not match.
_STABLE_NAME = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(flash-lite|flash|pro)$")
# The same, but allowing a suffix - used to read the versions already in the list.
_ANY_NAME = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(flash-lite|flash|pro)(?:-.*)?$")


def gemini_tier() -> str:
    """GEMINI_TIER, lower-cased (default "free"). Raises ValueError for an unknown tier."""
    tier = (os.environ.get("GEMINI_TIER") or DEFAULT_TIER).strip().lower()
    if tier not in TIERS:
        raise ValueError(f"GEMINI_TIER={tier!r} is not supported - use one of: {', '.join(TIERS)}.")
    return tier


def default_gemini_models() -> Tuple[str, ...]:
    """
    The models to use when GEMINI_MODELS is not set: the older GEMINI_MODEL
    if it is set, otherwise the list for GEMINI_TIER.
    Raises ValueError for an unknown GEMINI_TIER.
    """
    single = (os.environ.get("GEMINI_MODEL") or "").strip()
    if single:
        return (single,)
    return TIERS[gemini_tier()]


def gemini_settings() -> dict:
    """
    Gemini's extra provider settings (passed to GeminiProvider by
    app/ai/providers/__init__.py):
      tier      - "free" or "paid" (GEMINI_TIER)
      discovery - True when GEMINI_MODEL_DISCOVERY=auto
      extend    - False when the list was set by hand (GEMINI_MODELS / GEMINI_MODEL):
                  then discovery only removes retired models, never adds
    Raises ValueError for an unknown GEMINI_TIER or GEMINI_MODEL_DISCOVERY.
    """
    mode = (os.environ.get("GEMINI_MODEL_DISCOVERY") or DEFAULT_DISCOVERY).strip().lower()
    if mode not in DISCOVERY_MODES:
        raise ValueError(f"GEMINI_MODEL_DISCOVERY={mode!r} is not supported - use one of: {', '.join(DISCOVERY_MODES)}.")
    set_by_hand = any((os.environ.get(name) or "").strip() for name in ("GEMINI_MODELS", "GEMINI_MODEL"))
    return {"tier": gemini_tier(), "discovery": mode == "auto", "extend": not set_by_hand}


class RefreshResult(NamedTuple):
    """What refresh_model_list() decided: the new list, and what was added and removed."""
    models: List[str]
    added: List[str]
    removed: List[str]

    def summary(self) -> str:
        """One line for the server log."""
        changes = []
        if self.added:
            changes.append(f"added {', '.join(self.added)}")
        if self.removed:
            changes.append(f"removed {', '.join(self.removed)} (no longer offered)")
        what = "; ".join(changes) if changes else "no changes"
        return f"Gemini model list refreshed - {what}. Now: {' > '.join(self.models)}"


def refresh_model_list(available: Iterable[str], configured: Sequence[str], tier: str,
                       extend: bool = True) -> RefreshResult:
    """
    The updated model list (see the module docstring).

    `available`: the model names Google lists for this key that can generate
    content (without the "models/" prefix). `configured`: the current list,
    best first. `tier`: "free" or "paid". `extend`: whether newer models may
    be added (False for a list set by hand).

    If nothing would be left (every configured model gone and none added),
    the configured list is kept unchanged, so the app never ends up with no
    model - the fallback's 404 handling still skips models that are gone.
    """
    available = set(available)
    kept = [model for model in configured if model in available]
    removed = [model for model in configured if model not in available]

    added: List[str] = []
    if extend:
        families = TIER_FAMILIES[tier]
        newest_known = {}                                # family -> highest version already in the list
        for model in kept:
            parsed = _parse(model, _ANY_NAME)
            if parsed and parsed[1] in families:
                newest_known[parsed[1]] = max(newest_known.get(parsed[1], ()), parsed[0])
        candidates = []
        for model in available:
            parsed = _parse(model, _STABLE_NAME)
            if parsed is None or parsed[1] not in families or model in kept:
                continue
            version, family = parsed
            if family in newest_known and version <= newest_known[family]:
                continue                                 # not newer than what the list already has
            candidates.append((version, -families.index(family), model))
        added = [model for _, _, model in sorted(candidates, reverse=True)]   # newest first

    models = added + kept
    if not models:
        return RefreshResult(list(configured), [], [])
    return RefreshResult(models, added, removed)


def _parse(model: str, pattern) -> Optional[Tuple[Tuple[int, ...], str]]:
    """'gemini-3.8-flash' -> ((3, 8), 'flash'); None if the name does not match `pattern`."""
    match = pattern.match(model)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split(".")), match.group(2)

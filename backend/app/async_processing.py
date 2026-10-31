"""
Async collection of site statuses: sequential vs. concurrent (brief 5.1).

The task: before a shift, collect the current status of several org sites
(the Sites subsystem, app/sites.py) by their ids. Each lookup is one
independent task - it needs only its own site id, shares no state with the
others, and its result does not depend on any other lookup - so the lookups
can safely run at the same time.

The lookups are SIMULATED: each one waits with `await asyncio.sleep(delay)`
(never time.sleep, which would block the event loop) and then reads prepared
local data (SAMPLE_SITE_STATUSES, or a mapping passed in). There is no network
call, so the demo and the tests are fast and repeatable.

Two versions over the same data:

- collect_sequential(ids): awaits one lookup after another - the total time
  is the SUM of the delays.
- collect_concurrent(ids): creates every lookup as a task first, then waits
  for all of them with asyncio.gather - the total time is about the LONGEST
  delay, because the waits overlap.

Both return one SiteStatusResult per id, in the order of `ids` - even though
with different delays the concurrent lookups FINISH in a different order
(asyncio.gather keeps the input order). A lookup that fails (an unknown site
id) gives a result with `error` set and does not stop the others.

Usage (no event loop running yet, e.g. in main.py):

    import asyncio
    from app.async_processing import collect_concurrent, collect_sequential
    results = asyncio.run(collect_concurrent([1001, 1002, 1007]))
"""

import asyncio
import time
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

# Prepared local data: site id -> (status, simulated lookup delay in seconds).
# The ids are sites from data/sites.jsonl (1007 is the HR portal, whose
# .invalid address never answers - so Down); the delays differ on purpose, so
# concurrent lookups finish in a different order than they were started.
SAMPLE_SITE_STATUSES: Dict[int, Tuple[str, float]] = {
    1001: ("Operational", 0.30),
    1002: ("Operational", 0.10),
    1005: ("Degraded", 0.20),
    1007: ("Down", 0.05),
}


class SiteStatusResult(NamedTuple):
    site_id: int
    status: Optional[str]          # None when the lookup failed
    error: Optional[str] = None    # why it failed, else None


class Collection(NamedTuple):
    """The results of one collection run and how long it took."""
    results: List[SiteStatusResult]
    seconds: float
    finish_order: List[int]        # site ids in the order their lookups finished


async def fetch_site_status(site_id: int, statuses: Mapping[int, Tuple[str, float]],
                            finished: Optional[List[int]] = None) -> SiteStatusResult:
    """
    One independent task: wait the site's delay (simulated I/O), then return its
    status. Raises KeyError for an unknown site id (after no wait). Appends the
    id to `finished`, if given, when it completes - to show the finishing order.
    """
    status, delay = statuses[site_id]
    await asyncio.sleep(delay)
    if finished is not None:
        finished.append(site_id)
    return SiteStatusResult(site_id, status)


def _failed(site_id: int, exc: BaseException) -> SiteStatusResult:
    reason = "unknown site id" if isinstance(exc, KeyError) else f"{type(exc).__name__}: {exc}"
    return SiteStatusResult(site_id, None, reason)


async def collect_sequential(ids: Iterable[int], statuses: Optional[Mapping[int, Tuple[str, float]]] = None,
                             finished: Optional[List[int]] = None) -> List[SiteStatusResult]:
    """Look the sites up ONE AFTER ANOTHER (each await finishes before the next starts). Input order."""
    statuses = SAMPLE_SITE_STATUSES if statuses is None else statuses
    results = []
    for site_id in ids:
        try:
            results.append(await fetch_site_status(site_id, statuses, finished))
        except Exception as exc:               # one failure does not stop the others
            results.append(_failed(site_id, exc))
    return results


async def collect_concurrent(ids: Iterable[int], statuses: Optional[Mapping[int, Tuple[str, float]]] = None,
                             finished: Optional[List[int]] = None) -> List[SiteStatusResult]:
    """
    Look the sites up AT THE SAME TIME: every lookup is scheduled as a task
    before any result is collected, then asyncio.gather waits for all of them.
    Input order; a failed lookup becomes an error result (return_exceptions=True).
    """
    statuses = SAMPLE_SITE_STATUSES if statuses is None else statuses
    ids = list(ids)
    tasks = [asyncio.create_task(fetch_site_status(site_id, statuses, finished)) for site_id in ids]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    return [_failed(site_id, outcome) if isinstance(outcome, BaseException) else outcome
            for site_id, outcome in zip(ids, outcomes)]


def run_collection(collect, ids: Iterable[int],
                   statuses: Optional[Mapping[int, Tuple[str, float]]] = None) -> Collection:
    """Run `collect` (collect_sequential or collect_concurrent) once, timed - for main.py and the tests."""
    finished: List[int] = []
    started = time.perf_counter()
    results = asyncio.run(collect(ids, statuses, finished))
    return Collection(results, time.perf_counter() - started, finished)


__all__ = ["SAMPLE_SITE_STATUSES", "SiteStatusResult", "Collection", "fetch_site_status",
           "collect_sequential", "collect_concurrent", "run_collection"]

"""
Tests for app/async_processing.py: collecting site statuses sequentially and
concurrently (brief 5.1). The lookups only await asyncio.sleep() on prepared
data - no network. The delays are small, so the whole file runs in about a
second; the timing checks leave a wide margin for slow CI machines.

Run from backend/:  python -m pytest tests/test_async_processing.py -v
"""

import asyncio

from app.async_processing import (SAMPLE_SITE_STATUSES, SiteStatusResult, collect_concurrent, collect_sequential,
                                  run_collection)

# Delays DEcreasing along the input order: the first id is the slowest, so the
# concurrent lookups finish in exactly the reverse order of the input.
REVERSED_DELAYS = {1: ("Operational", 0.15), 2: ("Degraded", 0.10), 3: ("Down", 0.05)}
IDS = [1, 2, 3]
EXPECTED = [SiteStatusResult(1, "Operational"), SiteStatusResult(2, "Degraded"), SiteStatusResult(3, "Down")]


def test_both_versions_return_the_same_results_in_input_order():
    assert asyncio.run(collect_sequential(IDS, REVERSED_DELAYS)) == EXPECTED
    assert asyncio.run(collect_concurrent(IDS, REVERSED_DELAYS)) == EXPECTED


def test_concurrent_lookups_finish_out_of_order_but_results_keep_the_input_order():
    run = run_collection(collect_concurrent, IDS, REVERSED_DELAYS)
    assert run.finish_order == [3, 2, 1]                  # the fastest finished first
    assert [r.site_id for r in run.results] == IDS        # ... but the results are in input order


def test_sequential_lookups_finish_in_input_order():
    run = run_collection(collect_sequential, IDS, REVERSED_DELAYS)
    assert run.finish_order == IDS


def test_sequential_takes_the_sum_of_the_delays_and_concurrent_about_the_longest():
    sequential = run_collection(collect_sequential, IDS, REVERSED_DELAYS)
    concurrent = run_collection(collect_concurrent, IDS, REVERSED_DELAYS)
    assert sequential.seconds >= 0.30                     # 0.15 + 0.10 + 0.05
    assert 0.15 <= concurrent.seconds < 0.27              # about the longest (0.15), well under the sum
    assert concurrent.seconds < sequential.seconds


def test_a_failing_lookup_does_not_stop_the_others():
    ids = [1, 999, 3]                                      # 999 is not a known site
    for collect in (collect_sequential, collect_concurrent):
        results = asyncio.run(collect(ids, REVERSED_DELAYS))
        assert [r.site_id for r in results] == ids
        assert results[0].status == "Operational" and results[2].status == "Down"
        assert results[1] == SiteStatusResult(999, None, "unknown site id")


def test_the_default_data_covers_several_sites():
    results = asyncio.run(collect_concurrent(list(SAMPLE_SITE_STATUSES)))
    assert len(results) >= 3 and all(r.error is None for r in results)

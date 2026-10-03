"""
Unit tests for app.iterators.stale_in_progress_work - the generator
function with `yield` (Stage 1, Part D item 2). One test per behaviour the
instructions ask to demonstrate, plus the business condition itself.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.iterators import STALE_AFTER, stale_in_progress_work
from app.models import Fault, MaintenanceTask, ResolutionType, Role, SeverityCategory, User

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title, hours_since_update, in_progress=True):
    task = MaintenanceTask(title=title, description="d", created_by=reporter)
    if in_progress:
        task.start_progress()
    task.updated_at = NOW - timedelta(hours=hours_since_update)  # set AFTER start_progress(), which stamps "now"
    return task


def make_fault(reporter, title, hours_since_update):
    fault = Fault(title=title, description="d", created_by=reporter,
                  severity=SeverityCategory.MAJOR, severity_score=2.0)
    fault.start_progress()
    fault.updated_at = NOW - timedelta(hours=hours_since_update)
    return fault


@pytest.fixture
def incidents(reporter):
    closed = make_task(reporter, "Closed long ago", 50)
    closed.close(reporter, ResolutionType.RESOLVED, "done")
    closed.updated_at = NOW - timedelta(hours=50)
    return [
        make_task(reporter, "Fresh in progress", 1),                  # 1 - skipped (updated recently)
        make_task(reporter, "Old but never started", 10, in_progress=False),  # 2 - skipped (not in progress)
        make_task(reporter, "Stale task A", 5),                        # 3 - result 1
        closed,                                                         # 4 - skipped (closed)
        make_task(reporter, "Exactly at the limit", 4),                # 5 - skipped (not MORE than 4h)
        make_fault(reporter, "Stale fault B", 48),                     # 6 - result 2
        make_task(reporter, "Stale task C", 6),                        # 7 - result 3
    ]


def watched(items, seen):
    """Passes items through while recording which ones the generator has actually looked at."""
    for item in items:
        seen.append(item.title)
        yield item


# --- it really is a generator function ---

def test_it_is_a_generator_function_that_returns_a_generator(incidents):
    assert inspect.isgeneratorfunction(stale_in_progress_work)
    assert inspect.isgenerator(stale_in_progress_work(incidents, now=NOW))


# --- demonstration point 1: creating it processes nothing yet ---

def test_creating_the_generator_does_not_process_any_data(incidents):
    seen = []
    stale_in_progress_work(watched(incidents, seen), now=NOW)
    assert seen == []


# --- demonstration point 2: one read with next() ---

def test_one_next_call_returns_the_first_match_and_stops_there(incidents):
    seen = []
    gen = stale_in_progress_work(watched(incidents, seen), now=NOW)
    first = next(gen)
    assert first is incidents[2]
    assert seen == ["Fresh in progress", "Old but never started", "Stale task A"]  # items 4-7 not looked at yet


# --- demonstration points 3 + 4: a for loop continues from where next() stopped ---

def test_for_loop_continues_after_the_item_next_already_returned(incidents):
    gen = stale_in_progress_work(incidents, now=NOW)
    first = next(gen)
    rest = [i.title for i in gen]
    assert first.title == "Stale task A"
    assert rest == ["Stale fault B", "Stale task C"]   # "Stale task A" is not repeated


def test_the_generator_does_not_re_read_items_it_already_passed(incidents):
    seen = []
    gen = stale_in_progress_work(watched(incidents, seen), now=NOW)
    next(gen)
    looked_at_before = len(seen)
    next(gen)
    assert seen[looked_at_before:] == ["Closed long ago", "Exactly at the limit", "Stale fault B"]


# --- demonstration point 5: exhausted generators cannot be reused ---

def test_after_it_finishes_next_raises_stop_iteration(incidents):
    gen = stale_in_progress_work(incidents, now=NOW)
    list(gen)
    with pytest.raises(StopIteration):
        next(gen)


def test_an_exhausted_generator_yields_nothing_on_a_second_loop(incidents):
    gen = stale_in_progress_work(incidents, now=NOW)
    assert len(list(gen)) == 3
    assert list(gen) == []


def test_a_new_generator_starts_again_from_the_beginning(incidents):
    gen = stale_in_progress_work(incidents, now=NOW)
    list(gen)
    fresh = stale_in_progress_work(incidents, now=NOW)
    assert next(fresh).title == "Stale task A"


# --- the business condition ---

def test_only_stale_in_progress_incidents_are_yielded_and_they_are_the_same_objects(incidents):
    results = list(stale_in_progress_work(incidents, now=NOW))
    assert [r.title for r in results] == ["Stale task A", "Stale fault B", "Stale task C"]
    assert results[0] is incidents[2] and results[1] is incidents[5]


def test_exactly_at_the_limit_is_not_stale(reporter):
    at_limit = make_task(reporter, "At limit", STALE_AFTER.total_seconds() / 3600)
    assert list(stale_in_progress_work([at_limit], now=NOW)) == []


def test_the_threshold_can_be_changed(incidents):
    results = list(stale_in_progress_work(incidents, now=NOW, stale_after=timedelta(hours=1, minutes=30)))
    assert "Exactly at the limit" in [r.title for r in results]
    assert "Fresh in progress" not in [r.title for r in results]


def test_nothing_stale_gives_an_empty_result(reporter):
    assert list(stale_in_progress_work([make_task(reporter, "Fresh", 1)], now=NOW)) == []


def test_work_started_just_now_is_not_stale_with_the_real_clock(reporter):
    task = MaintenanceTask(title="Just started", description="d", created_by=reporter)
    task.start_progress()
    assert list(stale_in_progress_work([task])) == []

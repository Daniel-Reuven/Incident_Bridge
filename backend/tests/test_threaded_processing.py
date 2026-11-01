"""
Tests for app/threaded_processing.py: several technicians claiming the same
fault at the same moment, from separate threads (brief 5.2). The queue's
claim lock must let exactly one of them through.

The tests never assert WHICH thread wins - that is up to the scheduler - only
that exactly one does and that the final state is consistent. The races run
many rounds, so a missing lock would show up as a failure.

Run from backend/:  python -m pytest tests/test_threaded_processing.py -v
"""

import contextlib
import threading
import time

import pytest

from app.models.enums import IncidentStatus, Role, SeverityCategory
from app.models.fault import Fault
from app.models.user import User
from app.queues.fault_queue import FaultPriorityQueue
from app.threaded_processing import claim_most_severe_fault, claim_queued_fault, race_to_claim

ROUNDS = 25
TECHNICIANS = [User(username=f"tech{n}", role=Role.USER, password="Passw0rd!1") for n in range(1, 9)]
REPORTER = User(username="reporter", role=Role.USER, password="Passw0rd!1")


def make_fault(title="Login page down", severity=SeverityCategory.CRITICAL) -> Fault:
    return Fault(title=title, description="desc", created_by=REPORTER,
                 severity=severity, severity_score=float(severity.value), details={})


def claim_log_comments(fault: Fault) -> list:
    return [c for c in fault.comments if "Assigned to" in c.text]


def slow_claim(fault: Fault) -> Fault:
    """
    Make the claim's write step take 20 ms, as on a busy server: Fault.claim()
    checks "is it Open?" and THEN calls start_progress(), so every thread sits
    between the check and the write at the same time - exactly the window the
    lock must protect - instead of relying on luck to hit it.
    """
    write = fault.start_progress

    def slow():
        time.sleep(0.02)
        write()

    fault.start_progress = slow
    return fault


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_exactly_one_of_eight_simultaneous_claims_is_approved(round_number):
    queue, fault = FaultPriorityQueue(), slow_claim(make_fault())
    queue.push(fault)

    race = race_to_claim(queue, fault, TECHNICIANS)

    assert len(race.approved) == 1
    assert len(race.refused) == len(TECHNICIANS) - 1
    assert all("not waiting in the queue" in reason for _, reason in race.refused)
    winner = race.approved[0]
    assert fault.status == IncidentStatus.IN_PROGRESS
    assert fault.assigned_to.username == winner          # assigned to the approved one, not overwritten
    assert len(claim_log_comments(fault)) == 1          # one "Assigned to ..." log line, not one per thread
    assert fault not in queue


def test_without_the_lock_the_race_really_happens():
    # The same race with the lock replaced by a no-op: several threads pass the
    # check before any of them writes - the bug the lock prevents.
    queue, fault = FaultPriorityQueue(), slow_claim(make_fault())
    queue.push(fault)
    queue.claim_lock = contextlib.nullcontext()

    race = race_to_claim(queue, fault, TECHNICIANS)

    assert len(race.approved) > 1                           # more than one technician "got" the fault
    assert len(claim_log_comments(fault)) > 1               # and it carries several "Assigned to" lines


def test_claim_next_from_many_threads_hands_out_each_fault_once():
    queue = FaultPriorityQueue()
    faults = [make_fault(f"Fault {n}") for n in range(3)]
    for fault in faults:
        queue.push(fault)
    claimed, empty = [], []
    lock = threading.Lock()
    start = threading.Barrier(len(TECHNICIANS))

    def attempt(technician):
        start.wait()
        try:
            fault = claim_most_severe_fault(queue, technician)
        except IndexError:
            with lock:
                empty.append(technician.username)
        else:
            with lock:
                claimed.append((fault, technician.username))

    threads = [threading.Thread(target=attempt, args=(t,)) for t in TECHNICIANS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 3 and len(empty) == len(TECHNICIANS) - 3
    assert {fault.id for fault, _ in claimed} == {fault.id for fault in faults}   # no fault given out twice
    for fault, username in claimed:
        assert fault.assigned_to.username == username and len(claim_log_comments(fault)) == 1
    assert len(queue) == 0


def test_a_claim_waits_while_another_holds_the_lock():
    queue, fault = FaultPriorityQueue(), make_fault()
    queue.push(fault)
    done = threading.Event()

    def claim():
        claim_queued_fault(queue, fault, TECHNICIANS[0])
        done.set()

    with queue.claim_lock:                                  # another claim is "in the middle" of its steps
        thread = threading.Thread(target=claim)
        thread.start()
        assert not done.wait(0.2)                           # blocked: nothing happened yet
        assert fault.status == IncidentStatus.OPEN and fault in queue
    thread.join(2)
    assert done.is_set() and fault.assigned_to is TECHNICIANS[0]


def test_a_refused_claim_changes_nothing():
    queue, critical, minor = FaultPriorityQueue(), make_fault(), make_fault("Typo", SeverityCategory.MINOR)
    queue.push(critical)
    queue.push(minor)
    with pytest.raises(RuntimeError, match="higher-priority"):
        claim_queued_fault(queue, minor, TECHNICIANS[0])
    assert minor.status == IncidentStatus.OPEN and minor.assigned_to is None and minor in queue
    assert not queue.claim_lock.locked()                    # released even though the claim failed


def test_claim_next_on_an_empty_queue_raises_and_releases_the_lock():
    queue = FaultPriorityQueue()
    with pytest.raises(IndexError):
        claim_most_severe_fault(queue, TECHNICIANS[0])
    assert not queue.claim_lock.locked()

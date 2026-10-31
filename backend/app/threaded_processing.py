"""
Claiming a fault safely from several threads at once (brief 5.2).

The shared resource: the fault priority queue (state.faults, a
FaultPriorityQueue) and the open faults in it. Two technicians may press
"Claim" on the same open fault at the same moment, and FastAPI runs our
(sync) endpoints in a thread pool, so the two requests really do run in
parallel threads.

A claim is three steps: CHECK (is the fault still waiting, with nothing more
severe ahead of it? - FaultPriorityQueue.ensure_claimable), CLAIM (Open -> In
progress, assigned to the caller, log comment - Fault.claim) and REMOVE (take
it out of the queue). Without a lock, this interleaving is possible:

    thread A (tech1)                 thread B (tech2)
    ensure_claimable -> ok
                                     ensure_claimable -> ok   (A has not written yet)
    fault.claim: status is Open
                                     fault.claim: status is still Open
    status = In progress, tech1
                                     status = In progress, tech2   (overwrites A)
    queue.remove -> True
                                     queue.remove -> False (already gone)

Both requests get "200 OK", both technicians think the fault is theirs, the
fault is assigned to whoever wrote last and carries two "Assigned to ..." log
comments. "Claim next" has the same problem: both threads peek() the same
head of the queue.

The fix: ONE threading.Lock, owned by the queue (FaultPriorityQueue.claim_lock),
held around check + claim + remove, so they run as one step. The second
thread waits, then its check sees that the fault is no longer queued and is
refused (RuntimeError -> HTTP 409, or IndexError -> 404 for an empty queue).
Saving to the database and publishing the live-update event stay OUTSIDE the
lock (in app/api/incidents.py), and nothing inside it calls the network, so
the lock is held only for a few in-memory operations.

race_to_claim() is the demo: N threads, all started before any is joined,
try to claim the same fault; exactly one is approved.
"""

import threading
from typing import List, NamedTuple, Sequence, Tuple

from app.models.fault import Fault
from app.models.user import User
from app.queues.fault_queue import FaultPriorityQueue


def claim_queued_fault(queue: FaultPriorityQueue, fault: Fault, actor: User) -> None:
    """
    Claim one specific fault (its page's Claim button): check, claim and remove
    under the queue's claim lock. Raises RuntimeError, changing nothing, if the
    fault is not waiting in the queue, a more severe fault is waiting, or it is
    not Open.
    """
    with queue.claim_lock:
        queue.ensure_claimable(fault)
        fault.claim(actor)
        queue.remove(fault)


def claim_most_severe_fault(queue: FaultPriorityQueue, actor: User) -> Fault:
    """
    Claim the head of the queue ("Claim next"): peek, claim and remove under the
    queue's claim lock, and return the claimed fault. Raises IndexError if the
    queue is empty.
    """
    with queue.claim_lock:
        fault = queue.peek()
        fault.claim(actor)
        queue.remove(fault)
        return fault


class ClaimRace(NamedTuple):
    """The outcome of race_to_claim(): who got the fault, and who was refused and why."""
    approved: List[str]                   # usernames - exactly one when the lock works
    refused: List[Tuple[str, str]]        # (username, reason)


def race_to_claim(queue: FaultPriorityQueue, fault: Fault, technicians: Sequence[User]) -> ClaimRace:
    """
    Start one thread per technician, all trying to claim `fault` at the same
    moment (a Barrier releases them together), and wait for all of them. Every
    thread is started before any is joined. Which thread wins is up to the
    scheduler; that exactly one wins is guaranteed by the lock.
    """
    approved: List[str] = []
    refused: List[Tuple[str, str]] = []
    results_lock = threading.Lock()                    # guards the two result lists
    start_together = threading.Barrier(len(technicians))

    def attempt(technician: User) -> None:
        start_together.wait()
        try:
            claim_queued_fault(queue, fault, technician)
        except RuntimeError as exc:
            with results_lock:
                refused.append((technician.username, str(exc)))
        else:
            with results_lock:
                approved.append(technician.username)

    threads = [threading.Thread(target=attempt, args=(technician,), name=f"claim-{technician.username}")
               for technician in technicians]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return ClaimRace(approved, refused)


__all__ = ["claim_queued_fault", "claim_most_severe_fault", "ClaimRace", "race_to_claim"]

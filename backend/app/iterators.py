"""
Lazy processing pipeline over incidents (Stage 1, Part D item 3:
"Expression Generator and lazy processing pipeline").

The pipeline answers a real operational question: "which faults that are
still waiting for someone are serious enough (Critical or Major) to look
at first?" It is built from three chained generator expressions - no list
is ever created between the stages:

    stage 1: keep only OPEN faults                     (first condition)
    stage 2: keep only Critical/Major, convert each    (second filter + conversion)
             to a small (severity, title, id) tuple
    stage 3: turn each tuple into {id, severity, title, summary}   (summary field + id)

Nothing runs when the pipeline is built. Work only happens when a consumer
asks for the next value (next(), a for loop, islice, ...). See the README
section "Lazy pipeline" for the full explanation.
"""

from itertools import islice
from typing import Iterable, Iterator, List, Optional

from app.models import Fault, Incident, IncidentStatus, SeverityCategory


class ExaminedCounter:
    """
    Tiny helper that counts how many incidents the pipeline actually pulled
    from its source. It exists only to make laziness visible (in the demo,
    the tests and the API response): after consuming two results, the count
    shows how many incidents never had to be looked at.
    """

    def __init__(self) -> None:
        self.count = 0


def _counting(source: Iterable[Incident], counter: ExaminedCounter) -> Iterator[Incident]:
    """Generator that passes items through unchanged and counts each one pulled."""
    for item in source:
        counter.count += 1
        yield item


def urgent_fault_summaries(incidents: Iterable[Incident],
                           counter: Optional[ExaminedCounter] = None) -> Iterator[dict]:
    """
    Build (not run) the three-stage lazy pipeline and return its generator.

    `incidents` may be any iterable (e.g. IncidentRepository.list_all()).
    A generator can be consumed only once - call this function again to
    start over from the beginning.
    """
    source = _counting(incidents, counter) if counter is not None else incidents

    # Stage 1: open faults only.
    open_faults = (i for i in source if isinstance(i, Fault) and i.status == IncidentStatus.OPEN)
    # Stage 2: Critical/Major only, converted to a short tuple.
    urgent = ((f.severity.name, f.title, f.id)
              for f in open_faults if f.severity != SeverityCategory.MINOR)
    # Stage 3: the summary (plus the id, so a UI can link to the incident) returned to the caller.
    return ({"id": incident_id, "severity": severity, "title": title,
             "summary": f"[{severity}] {title} ({incident_id[:8]})"}
            for severity, title, incident_id in urgent)


def first_urgent_faults(incidents: Iterable[Incident], limit: int = 2) -> tuple:
    """
    Consume only the first `limit` results and stop. Returns
    (summaries, examined) where `examined` is how many incidents were
    pulled from the source to produce them - anything after that point was
    never touched.
    """
    counter = ExaminedCounter()
    summaries: List[dict] = list(islice(urgent_fault_summaries(incidents, counter), limit))
    return summaries, counter.count

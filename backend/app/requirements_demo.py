"""
The requirements demo page (frontend/demo.html, app/api/demo.py): every
Stage 1 requirement of the course brief, with

    brief    what the requirement asks, in our words
    demo     a short function the page can RUN - on fresh, in-memory data
             (the sample data from data/, no database, no network); what it
             passes to out() is shown under the Run button
    code     the app's own code that meets it (its source, read from the
             running code with inspect, so the page can never go out of date)
    uses     where the app uses it

The same ground as main.py, one requirement at a time. Nothing a demo does
touches the app's real state: its incidents, users, sites and lists are its
own objects, built for that run and then dropped. The four sample accounts
are made once per process (hashing their passwords takes a moment) and are
never changed by a demo.

Stage 2 entries are added after the team's Stage 2 work is merged.
"""

import functools
import importlib
import inspect
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.context import IncidentWorkSession, SiteCheckSession
from app.iterators import first_pressing_calls, stale_in_progress_work
from app.models import Fault, MaintenanceTask, Role, Site, User
from app.models.incident import Incident
from app.queues import FaultPriorityQueue, MaintenanceQueue
from app.reports import build_site_report, count_by
from app.repository import IncidentRepository, UserStore
from app.services import FakeChecker, OutboxNotifier
from app.services.notifier import Notifier
from app.services.severity_scoring import SeverityScorer
from app.sites import SiteDirectory

DATA = Path(__file__).resolve().parents[2] / "data"
BACKEND = Path(__file__).resolve().parents[1]
DEMO_SITE_ID = 990001          # never a real site id: SiteCheckSession's lock is shared with the app

Out = Callable[[str], None]


@dataclass(frozen=True)
class Use:
    text: str
    link: Optional[str] = None          # a page of the app, when there is one to open


@dataclass(frozen=True)
class Requirement:
    id: str
    title: str
    brief: str
    demo: Callable[[Out], None]
    code: Tuple[str, ...]               # "module:qualified.name"
    uses: Tuple[Use, ...]


# --- the sample data, fresh for every run -------------------------------------------------------

_SAMPLE_USERS = (("admin", "Passw0rd!1", "admin"), ("tech1", "Passw0rd!2", "user"),
                 ("tech2", "Passw0rd!3", "user"), ("tech3", "Passw0rd!4", "user"))
_users: List[User] = []
_users_lock = threading.Lock()


def sample_users() -> UserStore:
    """The four sample accounts (the usernames data/sample_data.jsonl uses), in a new UserStore."""
    with _users_lock:
        if not _users:
            _users.extend(User.from_dict({"username": u, "password": p, "role": r}) for u, p, r in _SAMPLE_USERS)
    store = UserStore()
    for user in _users:
        store.add(user)
    return store


def sample_incidents(users: UserStore) -> IncidentRepository:
    """data/sample_data.jsonl, loaded into a new in-memory repository (no database)."""
    path = DATA / "sample_data.jsonl"
    if not path.is_file():
        raise RuntimeError(f"The sample data is not here ({path}) - the demos need the repository's data/ folder.")
    incidents = IncidentRepository()
    incidents.load_from_jsonl(path, users)
    return incidents


def new_fault(title: str, reporter: User, details: dict) -> Fault:
    """A fault the way the app creates one: SeverityScorer decides the severity from the details."""
    severity = SeverityScorer.score(details)
    return Fault(title, "Reported in the demo", reporter, severity=severity, severity_score=float(severity.value),
                 details=details)


def of_kind(incidents: IncidentRepository, kind: str, status: Optional[str] = None) -> list:
    return [i for i in incidents.list_all() if i.kind == kind and (status is None or i.status.value == status)]


# --- the demos ------------------------------------------------------------------------------------

def demo_classes(out: Out) -> None:
    users = sample_users()
    admin = users.get("admin")
    task = MaintenanceTask("Rotate backup tapes", "Weekly rotation", admin)        # constructor
    out(f"str(task)   -> {task}")
    out(f"repr(task)  -> {task!r}")
    user = User.from_dict({"username": "dlevi", "password": "Aboard-Today#7", "role": "user"})   # @classmethod
    out(f"User.from_dict(...) -> {user!r}")
    out(f"str(user)   -> {user}")


def demo_oop(out: Out) -> None:
    users = sample_users()
    admin = users.get("admin")
    task = MaintenanceTask("Patch the web server", "Monthly patches", admin)
    fault = new_fault("Payment API is down", admin, {"system_unavailable": True})
    out(f"Inheritance: Fault -> {' -> '.join(c.__name__ for c in Fault.__mro__[1:-1])}")
    out("Polymorphism - one loop, no type checks; each class overrides kind and extra_fields():")
    for incident in (task, fault):
        out(f"  {incident.kind:<12} {incident.extra_fields()}")
    task.add_comment(admin, "Scheduled for Sunday night.")
    out(f"Composition: the task HAS {len(task.comments)} Comment object(s): {task.comments[0].text!r}")
    queue = MaintenanceQueue()
    queue.enqueue(task)
    out(f"Composition: the queue HAS the task -> {queue}")


def demo_abstract(out: Out) -> None:
    users = sample_users()
    try:
        Incident("A title", "A description", users.get("admin"))                   # abstract base class
    except TypeError as exc:
        out(f"Incident(...) -> TypeError: {exc}")
    checker = FakeChecker({"https://shop.example.com": [503, None]})               # an AvailabilityChecker
    for _ in range(2):
        result = checker.check("https://shop.example.com")
        out(f"FakeChecker.check -> HTTP {result.http_status}, observed {result.observed_status.value}")
    try:
        Notifier()                                                                 # another interface
    except TypeError as exc:
        out(f"Notifier() -> TypeError: {exc}")
    out(f"OutboxNotifier is a Notifier: {issubclass(OutboxNotifier, Notifier)} - the site directory takes any one")


def demo_validation(out: Out) -> None:
    admin = sample_users().get("admin")
    attempts = [
        ("blank title", lambda: MaintenanceTask("   ", "d", admin)),
        ("site with an ftp:// address", lambda: Site(5000, "Files", "ftp://files.example.com")),
        ("weak password", lambda: User("newbie", Role.USER, "short")),
        ("unknown fault detail", lambda: SeverityScorer.validate_details({"system_down": True})),
        ("percentage over 100", lambda: SeverityScorer.validate_details({"affected_users_percent": 140})),
    ]
    for description, attempt in attempts:
        try:
            attempt()
            out(f"{description:<28} -> accepted")
        except ValueError as exc:
            out(f"{description:<28} -> ValueError: {exc}")


def demo_lists_tuples(out: Out) -> None:
    incidents = sample_incidents(sample_users())
    faults = of_kind(incidents, "fault")                                   # a list
    first, second, *rest = sorted(faults, key=lambda f: f.severity.value)   # * collects the rest
    out(f"first:  {first.severity.name} - {first.title}")
    out(f"second: {second.severity.name} - {second.title}")
    out(f"rest:   {len(rest)} more faults")
    calls, examined = first_pressing_calls(incidents.list_all(), limit=2,
                                           now=datetime.now(timezone.utc) + timedelta(days=4))
    out(f"a function returning a tuple, unpacked: {len(calls)} calls, {examined} incidents examined")
    point = ("seed-003", "Payment API is down")                             # a fixed record: tuple
    incident_id, title = point
    out(f"tuple unpacking: id={incident_id}, title={title!r}")


def demo_sets(out: Out) -> None:
    users = sample_users()
    incidents = sample_incidents(users)
    technicians = {u.username for u in users.all_users() if u.role == Role.USER}     # set comprehension
    reporters = {i.created_by.username for i in of_kind(incidents, "maintenance")}
    out(f"technicians                     {sorted(technicians)}")
    out(f"reported maintenance tasks      {sorted(reporters & technicians)}   (intersection)")
    out(f"never reported one              {sorted(technicians - reporters)}   (difference)")
    out(f"technicians and reporters       {sorted(technicians | reporters)}   (union)")
    directory = SiteDirectory()
    directory.load_mailing_lists_from_lines(
        ['{"list_id": "ops", "name": "Ops", "members": ["a@example.com", "A@example.com", "b@example.com"]}'])
    members = directory.get_mailing_list("ops").members
    out(f"a mailing list keeps a set of members - the duplicate address counted once: {members}")


def demo_dicts(out: Out) -> None:
    users = sample_users()
    incidents = sample_incidents(users)
    out(f"lookup by id:        incidents.get('seed-003').title = {incidents.get('seed-003').title!r}")
    out(f"missing key, .get(): users.get('nobody') = {users.get('nobody')}")
    counts = {}
    for fault in of_kind(incidents, "fault"):                       # counting dict
        counts[fault.severity.name] = counts.get(fault.severity.name, 0) + 1
    for severity, count in counts.items():                         # .items() with unpacking
        out(f"  {severity:<9} {count}")
    by_creator = {}                                                 # grouping dict
    for incident in incidents.list_all():
        by_creator.setdefault(incident.created_by.username, []).append(incident.id)
    out(f"grouped by reporter: { {name: len(ids) for name, ids in sorted(by_creator.items())} }")
    again = incidents.load_from_jsonl(DATA / "sample_data.jsonl", users)   # the same file twice
    out(f"loading the file again: {again.created} created, {len(again.skipped_duplicate_ids)} duplicate ids skipped")


def demo_deque(out: Out) -> None:
    users = sample_users()
    admin = users.get("admin")
    queue = MaintenanceQueue()
    for title in ("Patch", "Reboot", "Verify"):
        queue.enqueue(MaintenanceTask(title, "In this order", admin))          # deque.append
    out(f"pending, in arrival order: {[t.title for t in queue]}")
    while True:
        try:
            task = queue.start_next()                                          # deque.popleft
        except IndexError as exc:                                              # empty queue: handled
            out(f"start_next() on an empty queue -> IndexError: {exc}")
            break
        queue.complete_current(admin, "Done.")
        out(f"started and completed: {task.title}")


def demo_heapq(out: Out) -> None:
    users = sample_users()
    admin = users.get("admin")
    queue = FaultPriorityQueue()
    for title, details in [("Typo on the About page", {}),
                           ("Payment API is down", {"system_unavailable": True}),
                           ("Reports are slow", {"affected_users_percent": 30}),
                           ("Login fails for everyone", {"system_unavailable": True})]:
        queue.push(new_fault(title, admin, details))                          # heapq.heappush
    out(f"in the queue: {len(queue)} faults; most severe first (1 = Critical), ties by arrival:")
    while len(queue):
        fault = queue.pop_most_severe()                                        # heapq.heappop
        out(f"  {fault.severity.value} {fault.severity.name:<9} {fault.title}")


def fault_sort_key(fault: Fault) -> tuple:
    """Severity first (1 = Critical), then title - a two-field key."""
    return fault.severity.value, fault.title


def demo_sorting(out: Out) -> None:
    incidents = sample_incidents(sample_users())
    faults = [i for i in incidents.list_all() if i.kind == "fault"]               # list comprehension
    out("sorted with a named function (severity, then title):")
    for fault in sorted(faults, key=fault_sort_key)[:3]:
        out(f"  {fault.severity.name:<9} {fault.title}")
    longest = sorted(faults, key=lambda f: len(f.title), reverse=True)[:2]         # lambda as key
    out(f"two longest titles (lambda): {[f.title for f in longest]}")
    by_id = {i.id: i.title for i in incidents.list_all()}                         # dict comprehension
    out(f"dict comprehension: {len(by_id)} entries, seed-001 -> {by_id['seed-001']!r}")
    reporters = {i.created_by.username for i in incidents.list_all()}              # set comprehension
    out(f"set comprehension (reporters): {sorted(reporters)}")
    out(f"count_by (zero-filled, fixed order): {count_by((f.severity.name for f in faults), ('CRITICAL', 'MAJOR', 'MINOR'))}")


def demo_iterator(out: Out) -> None:
    admin = sample_users().get("admin")
    queue = MaintenanceQueue()
    for title in ("First", "Second", "Third"):
        queue.enqueue(MaintenanceTask(title, "d", admin))
    first, second = iter(queue), iter(queue)              # every iter() builds a NEW iterator
    out(f"iter(queue) -> {type(first).__name__} (a class with __iter__ and __next__)")
    out(f"first:  next() -> {next(first).title}, next() -> {next(first).title}")
    out(f"second: next() -> {next(second).title}   <- independent, starts from the beginning")
    out(f"first:  the rest -> {[t.title for t in first]}")
    try:
        next(first)
    except StopIteration:
        out("first:  next() again -> StopIteration (exhausted)")
    out(f"second: {second.remaining} item(s) still to come")


def demo_generator(out: Out) -> None:
    admin = sample_users().get("admin")
    now = datetime.now(timezone.utc)
    work = []
    for title, hours in (("Firewall review", 1), ("Backup restore test", 6), ("Not started", None),
                         ("Certificate renewal", 30)):
        task = MaintenanceTask(title, "d", admin)
        if hours is not None:
            task.start_progress()
            task.updated_at = now - timedelta(hours=hours)
        work.append(task)
    stale = stale_in_progress_work(work, now)                       # nothing runs yet: a generator object
    out(f"created: {type(stale).__name__} - no incident read yet")
    out(f"next() -> {next(stale).title}")
    out(f"for loop resumes after it -> {[t.title for t in stale]}")
    out(f"exhausted: a second loop gives {list(stale)}")
    out(f"a NEW generator starts over -> {next(stale_in_progress_work(work, now)).title}")


def demo_pipeline(out: Out) -> None:
    incidents = sample_incidents(sample_users())
    total = sum(1 for _ in incidents.list_all())
    later = datetime.now(timezone.utc) + timedelta(days=4)          # as if four days had passed
    calls, examined = first_pressing_calls(incidents.list_all(), limit=2, now=later)
    for call in calls:
        out(f"result: {call['summary']}")
    out(f"stopped after 2 results: examined {examined} of {total} incidents - "
        f"{total - examined} never touched")


def demo_jsonl(out: Out) -> None:
    users = sample_users()
    incidents = IncidentRepository()
    result = incidents.load_from_jsonl(DATA / "sample_data.jsonl", users)
    out(f"data/sample_data.jsonl: {result.created} loaded, {len(result.skipped_duplicate_ids)} duplicates, "
        f"{len(result.skipped_invalid)} invalid")
    lines = [
        '{"kind": "maintenance", "title": "No id", "description": "d", "created_by": "admin"}',
        '{"id": "x-1", "kind": "fault", "title": "Bad", "description": "d", "created_by": "admin",'
        ' "details": {"affected_users_percent": "lots"}}',
        '{"id": "x-2", "kind": "maintenance", "title": "Who?", "description": "d", "created_by": "ghost"}',
        '[1, 2, 3]',
        '{not json',
        '{"id": "x-3", "kind": "maintenance", "title": "Fine", "description": "d", "created_by": "tech1"}',
    ]
    check = IncidentRepository().load_from_jsonl_lines(lines, users)
    out(f"six lines: {check.created} loaded; every bad one skipped with its reason, nothing crashes:")
    for reason in check.skipped_invalid:
        out(f"  - {reason}")


def demo_context(out: Out) -> None:
    admin = sample_users().get("admin")
    task = MaintenanceTask("Disk cleanup", "d", admin)
    with IncidentWorkSession(task, admin):
        out(f"inside: status {task.status.value}")
    out(f"normal exit -> comment: {task.comments[-1].text}")
    other = MaintenanceTask("Disk resize", "d", admin)
    try:
        with IncidentWorkSession(other, admin):
            raise RuntimeError("disk full")
    except RuntimeError as exc:
        out(f"exception -> comment: {other.comments[-1].text}")
        out(f"              and the exception still reaches the caller: {exc}")
    site = Site(DEMO_SITE_ID, "Demo site", "https://demo.example.com")
    with SiteCheckSession(site):
        try:
            with SiteCheckSession(site):
                pass
        except RuntimeError as exc:
            out(f"SiteCheckSession: a second check of the same site is refused - {exc}")
    out(f"after the with block: is_running = {SiteCheckSession.is_running(DEMO_SITE_ID)} (released)")


def demo_subsystem(out: Out) -> None:
    users = sample_users()
    incidents = sample_incidents(users)
    admin = users.get("admin")
    directory = SiteDirectory(checker=FakeChecker({"https://hr.incident-bridge.invalid": [None, None]}),
                              notifier=OutboxNotifier(log=lambda line: out(f"  [outbox] {line}")))
    directory.load_sites_from_jsonl(DATA / "sites.jsonl")
    directory.load_mailing_lists_from_jsonl(DATA / "mailing_lists.jsonl")
    for round_number in (1, 2):
        directory.check_all()
        out(f"check round {round_number}: Site 1007 is {directory.get_site(1007).status.value}")
    draft = next(n for n in directory.notifications() if n.site_id == 1007 and n.state.value == "draft")
    directory.send_notification(draft.id, admin, message="The HR portal is down - we are on it.")
    out(f"the draft for Site 1007 was sent to {', '.join(draft.list_ids)}")
    for line in build_site_report(directory, incidents.list_all()).lines()[:12]:
        out(line)


# --- the list the page shows ----------------------------------------------------------------------

REQUIREMENTS: Tuple[Requirement, ...] = (
    Requirement(
        "classes", "Classes, constructors, __str__ and __repr__",
        "Model the domain with classes: a constructor that sets up a valid object, a readable __str__ for "
        "people and an unambiguous __repr__ for developers, and an alternative constructor (@classmethod) "
        "that builds an object from a dict read from a file.",
        demo_classes,
        ("app.models.maintenance_task:MaintenanceTask.__init__", "app.models.incident:Incident.__repr__",
         "app.models.maintenance_task:MaintenanceTask.__str__", "app.models.user:User.from_dict"),
        (Use("Every incident, user, site and list in the app is one of these objects; the dashboard and the "
             "incident page show them.", "/dashboard.html"),
         Use("from_dict builds them from data/*.jsonl and from Import seed data."))),
    Requirement(
        "oop", "Composition, inheritance, overriding and polymorphism",
        "Base and derived classes that override behaviour, objects that contain other objects, and code that "
        "treats different classes the same way through a shared interface.",
        demo_oop,
        ("app.models.fault:Fault.kind", "app.models.fault:Fault.extra_fields",
         "app.models.maintenance_task:MaintenanceTask.extra_fields", "app.models.incident:Incident.add_comment"),
        (Use("The API turns any incident into JSON with one function, using kind and extra_fields() - no type "
             "checks (app/api/serializers.py).", "/dashboard.html"),
         Use("Queues hold incidents, incidents hold their comments and history, the site directory holds sites "
             "and lists."))),
    Requirement(
        "abstract", "Abstract classes and interfaces",
        "A class that only defines what its subclasses must provide and cannot be created itself, with more "
        "than one implementation that the code can swap.",
        demo_abstract,
        ("app.models.incident:Incident", "app.services.availability:AvailabilityChecker",
         "app.services.availability:FakeChecker.check", "app.services.notifier:Notifier"),
        (Use("Site checks use HttpChecker in the app and FakeChecker in the tests and here, behind the same "
             "AvailabilityChecker interface.", "/sites.html"),
         Use("Notifications go through a Notifier (OutboxNotifier records them)."))),
    Requirement(
        "validation", "Validation with ValueError",
        "Refuse invalid input where it enters the model, with a clear ValueError, so no invalid object ever "
        "exists.",
        demo_validation,
        ("app.models.incident:_validated_text", "app.models.site:Site.site_url",
         "app.services.severity_scoring:SeverityScorer.validate_details"),
        (Use("The API turns every ValueError into a 400 answer with its message - e.g. a blank title in "
             "New incident.", "/dashboard.html"),
         Use("Password rules on the Users page and in Settings come from PasswordPolicy.", "/users.html"))),
    Requirement(
        "lists-tuples", "Lists, tuples and * unpacking",
        "Use lists for ordered, changing collections and tuples for fixed records; unpack them, including "
        "with * to collect the rest.",
        demo_lists_tuples,
        ("app.iterators:first_pressing_calls", "app.reports:SiteReportRow"),
        (Use("Pressing maintenance calls returns (calls, examined) as a tuple.", "/pressing.html"),
         Use("The site report's rows are fixed records with tuples of incident and list ids.", "/sites.html"))),
    Requirement(
        "sets", "Set operations",
        "Use sets for membership and for union, intersection and difference.",
        demo_sets,
        ("app.models.mailing_list:MailingList.add_member", "app.reports:build_site_report"),
        (Use("The site portal's summary: outages no incident mentions (difference), sites that look fine "
             "although incidents mention them (intersection), sites no list covers.", "/sites.html"),
         Use("A mailing list keeps its members as a set, so an address is there once."))),
    Requirement(
        "dicts", "Dictionaries: lookup, grouping, counting, .items(), .get(), duplicates",
        "Look items up by key, group and count with a dict, loop over .items(), use .get() where a key may "
        "be missing, and detect duplicate ids.",
        demo_dicts,
        ("app.reports:count_by", "app.repository:IncidentRepository.get"),
        (Use("Every incident, user and site is found by its id in a dict.", "/dashboard.html"),
         Use("Import seed data skips incidents whose id is already there.", "/dashboard.html"),
         Use("The site portal counts sites per status.", "/sites.html"))),
    Requirement(
        "deque", "A deque as a FIFO queue, and an empty queue",
        "Maintenance tasks are done strictly in arrival order: a deque appends at the back and takes from "
        "the front; taking from an empty queue is handled, not a crash.",
        demo_deque,
        ("app.queues.maintenance_queue:MaintenanceQueue.enqueue", "app.queues.maintenance_queue:MaintenanceQueue.start_next"),
        (Use("The dashboard's maintenance queue and Start next.", "/dashboard.html"),
         Use("An empty queue answers 404 \"no pending maintenance tasks\" instead of an error."))),
    Requirement(
        "heapq", "A priority queue with heapq",
        "Faults are taken most severe first (ties: the earlier one first), using heapq.",
        demo_heapq,
        ("app.queues.fault_queue:FaultPriorityQueue.push", "app.queues.fault_queue:FaultPriorityQueue.pop_most_severe"),
        (Use("The dashboard's fault queue and Claim next - one shared queue for every technician.",
             "/dashboard.html"),),),
    Requirement(
        "sorting", "Comprehensions and sorting",
        "List, dict and set comprehensions; sorted() with a named key function, with a lambda, and with a "
        "two-field key.",
        demo_sorting,
        ("app.reports:attention_key", "app.reports:count_by"),
        (Use("The site report is sorted with attention_key: status urgency, then incident count, then id.",
             "/sites.html"),
         Use("The dashboard sorts by severity, status or report time.", "/dashboard.html"))),
    Requirement(
        "iterator", "An Iterable with a separate Iterator class",
        "A collection whose __iter__ returns a NEW iterator object of its own class (with __next__ and "
        "StopIteration), so two loops over it are independent.",
        demo_iterator,
        ("app.iterators:SnapshotIterator", "app.iterators:FifoQueueIterator",
         "app.queues.maintenance_queue:MaintenanceQueue.__iter__"),
        (Use("The dashboard's queue lists come from iterating the queues (GET /incidents/maintenance/queue).",
             "/dashboard.html"),),),
    Requirement(
        "generator", "A generator with yield",
        "A generator function that hands back matching items one at a time, only when asked: next(), a for "
        "loop that resumes after it, exhaustion, and a new generator to start over.",
        demo_generator,
        ("app.iterators:stale_in_progress_work",),
        (Use("Check stale incidents on the dashboard: work in progress with no update for a set time.",
             "/dashboard.html"),),),
    Requirement(
        "pipeline", "A lazy pipeline that stops early",
        "Chain generator stages so that asking for the first two results reads only as much input as needed - "
        "the rest is never touched.",
        demo_pipeline,
        ("app.iterators:pressing_maintenance_calls", "app.iterators:first_pressing_calls"),
        (Use("Pressing maintenance calls: open tasks waiting more than three days.", "/pressing.html"),),),
    Requirement(
        "jsonl", "Reading JSONL with validation",
        "Read records line by line from a JSONL file into dicts and then objects; skip a bad line with its "
        "reason instead of stopping.",
        demo_jsonl,
        ("app.repository:IncidentRepository.load_from_jsonl_lines",),
        (Use("Import seed data on the dashboard (admin) uses the same loader and shows what was skipped.",
             "/dashboard.html"),
         Use("Sites and mailing lists can be loaded from data/ at startup."))),
    Requirement(
        "context", "A custom context manager",
        "A class with __enter__ and __exit__ that sets something up and always cleans up - also when the "
        "block raises, without hiding the exception.",
        demo_context,
        ("app.context:IncidentWorkSession", "app.context:SiteCheckSession"),
        (Use("SiteCheckSession guards every site check, so the same site is never checked twice at once.",
             "/sites.html"),
         Use("IncidentWorkSession is not used by the web app (its actions record their own history); it is "
             "shown here, in main.py and in the tests."))),
    Requirement(
        "subsystem", "The group-of-four subsystem and its report",
        "A second subsystem linked to the incidents - sites and mailing lists - with its processes, states, "
        "cross-reference checks and a report.",
        demo_subsystem,
        ("app.sites:SiteDirectory.check_all", "app.site_scan:scan_incidents", "app.reports:build_site_report"),
        (Use("The site portal (admin): checks, statuses, notifications to the lists, the report.", "/sites.html"),)),
)

_BY_ID = {r.id: r for r in REQUIREMENTS}


# --- what the page needs -----------------------------------------------------------------------------

def _resolve(ref: str):
    module_name, qualname = ref.split(":")
    target = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return inspect.unwrap(getattr(target, "fget", target))      # a property: its getter


def source_of(ref: str) -> dict:
    """{"ref", "file", "line", "source"} - the code as it runs now."""
    target = _resolve(ref)
    lines, start = inspect.getsourcelines(target)
    path = Path(inspect.getsourcefile(target)).resolve()
    try:
        shown = path.relative_to(BACKEND.parent).as_posix()
    except ValueError:
        shown = path.name
    return {"ref": ref.split(":")[1], "file": shown, "line": start, "source": _dedent(lines)}


def _dedent(lines: List[str]) -> str:
    return textwrap.dedent("".join(lines)).rstrip()


@functools.lru_cache(maxsize=None)
def describe_all() -> Tuple[dict, ...]:
    """Every requirement for the page - read once (the code does not change while the app runs)."""
    return tuple(describe(r) for r in REQUIREMENTS)


def describe(requirement: Requirement) -> dict:
    return {
        "id": requirement.id,
        "title": requirement.title,
        "brief": requirement.brief,
        "demo": {"file": "backend/app/requirements_demo.py", "source": _dedent(inspect.getsourcelines(requirement.demo)[0])},
        "code": [source_of(ref) for ref in requirement.code],
        "uses": [{"text": use.text, "link": use.link} for use in requirement.uses],
    }


_run_lock = threading.Lock()


def run(requirement_id: str) -> dict:
    """
    Run one demo on fresh in-memory data: {"output": [...], "error": None | text, "ms": n}.
    One at a time: the demos are quick, and the context-manager one holds its site's check lock.
    """
    requirement = _BY_ID.get(requirement_id)
    if requirement is None:
        raise KeyError(f"No requirement {requirement_id!r}.")
    output: List[str] = []
    error = None
    with _run_lock:
        started = time.perf_counter()
        try:
            requirement.demo(output.append)
        except Exception as exc:                # a demo that breaks shows why, it does not break the page
            error = f"{type(exc).__name__}: {exc}"
    return {"output": output, "error": error, "ms": round((time.perf_counter() - started) * 1000)}

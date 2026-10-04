"""
Incident Bridge - Stage 1 demo scenario.

Run from the repository root (where it belongs, per the brief):

    python main.py

Data is always read from <repository root>/data, found by walking up from
this file's folder (see find_repo_root), so it does not matter which folder
you run it from.

What it does: loads the AI-generated data in data/ and walks through every
Stage 1 requirement, one titled section at a time, printing what happens:

    1. Loading the data files (JSONL -> dicts -> objects, with validation)
    2. The OOP model (__str__/__repr__, polymorphism, validation errors)
    3. Data structures (deque FIFO, heapq priority queue, dict, set, tuple)
    4. Sorting and comprehensions
    5. Iterable and Iterator classes (two independent iterators)
    6. Generator with yield (next, for, resume, exhaustion)
    7. Lazy pipeline (stops after 2 results)
    8. Context managers (normal exit and exit by exception)
    9. Sites & Mailing Lists subsystem (process, notifications, scan, report)

It runs completely offline: no web server, no database and no network. All
data lives in memory, site availability checks use FakeChecker (scripted
results instead of real HTTP requests) and notifications use OutboxNotifier
(recorded, not emailed). Nothing on disk is changed.

This file defines no business classes - per the brief it only USES the
domain code in backend/app. The small functions below only print things or
build demo input.

Requirements: Python 3.9+ and the backend's dependencies installed
(cd backend && pip install -e ".[dev]"). The web app itself is started
separately with backend/serve_app.py - see README.md, section 15.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path



def find_repo_root(start: Path) -> Path:
    """
    The repository root: the first folder, starting from this file's own
    folder and walking up, that contains both backend/app and data/. This
    makes the paths independent of where main.py is placed and of the
    folder it is run from - data/ is always read from the repository root,
    exactly like the web app does.
    """
    for folder in (start, *start.parents):
        if (folder / "backend" / "app").is_dir() and (folder / "data").is_dir():
            return folder
    raise SystemExit(f"main.py: cannot find the repository root (a folder containing backend/app and data/) "
                     f"above {start}")


ROOT = find_repo_root(Path(__file__).resolve().parent)
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT / "backend"))  # make the `app` package importable wherever main.py is run from

# Some consoles (e.g. older Windows terminals) cannot print every character in
# the sample data; replace those instead of crashing.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from app.context import IncidentWorkSession, SiteCheckSession  # noqa: E402
from app.iterators import first_pressing_calls, stale_in_progress_work  # noqa: E402
from app.models import MaintenanceTask, Role, Site, User  # noqa: E402
from app.queues import FaultPriorityQueue, MaintenanceQueue  # noqa: E402
from app.reports import build_site_report  # noqa: E402
from app.repository import IncidentRepository, UserStore  # noqa: E402
from app.services import FakeChecker, OutboxNotifier  # noqa: E402
from app.sites import SiteDirectory  # noqa: E402

# Demo accounts - the same usernames the sample data uses. In the web app they
# come from INCIDENT_BRIDGE_USERS in backend/.env instead.
DEMO_USERS = [
    {"username": "admin", "password": "Passw0rd1", "role": "admin"},
    {"username": "tech1", "password": "Passw0rd2", "role": "user"},
    {"username": "tech2", "password": "Passw0rd3", "role": "user"},
    {"username": "tech3", "password": "Passw0rd4", "role": "user"},
]

# Scripted availability results for FakeChecker: an int is an HTTP status
# code, None is "no response". Sites not listed always answer 200.
HR_PORTAL_URL = "https://hr.incident-bridge.invalid"        # Site 1007
DEMO_SITE_URL = "https://status-demo.example.com"           # created in section 9
CHECK_SCRIPT = {HR_PORTAL_URL: [None, None], DEMO_SITE_URL: [None, None]}


def section(number: int, title: str) -> None:
    print(f"\n{'=' * 72}\n{number}. {title}\n{'=' * 72}")


def show(label: str, value="") -> None:
    print(f"  {label}{': ' if value != '' else ''}{value}")


def titles(items) -> list:
    return [item.title for item in items]


# ---------------------------------------------------------------------------

def load_data():
    section(1, "Loading the data files (JSONL -> dict -> object)")
    users = UserStore()
    for entry in DEMO_USERS:
        users.add(User.from_dict(entry))                      # alternative constructor (@classmethod)
    show("Users", ", ".join(sorted(u.username for u in users.all_users())))

    incidents = IncidentRepository()                          # memory-only: no database
    result = incidents.load_from_jsonl(DATA / "sample_data.jsonl", users)
    show("data/sample_data.jsonl", f"{result.created} incidents loaded, "
                                   f"{len(result.skipped_duplicate_ids)} duplicates, "
                                   f"{len(result.skipped_invalid)} invalid")

    bad_lines = [
        '{"kind": "maintenance", "title": "No id", "description": "d", "created_by": "admin"}',
        '{"id": "x-1", "kind": "fault", "title": "Bad", "description": "d", "created_by": "admin",'
        ' "details": {"affected_users_percent": "lots"}}',
        '{"id": "x-2", "kind": "maintenance", "title": "Who?", "description": "d", "created_by": "ghost"}',
        '[1, 2, 3]',
        '{not json',
    ]
    check = IncidentRepository().load_from_jsonl_lines(bad_lines, users)
    show("Invalid records are skipped with a reason, never crash the load")
    for reason in check.skipped_invalid:
        print(f"    - {reason}")

    directory = SiteDirectory(checker=FakeChecker(CHECK_SCRIPT),
                              notifier=OutboxNotifier(log=lambda line: print(f"    [outbox] {line}")))
    show("data/sites.jsonl", directory.load_sites_from_jsonl(DATA / "sites.jsonl"))
    show("data/mailing_lists.jsonl", directory.load_mailing_lists_from_jsonl(DATA / "mailing_lists.jsonl"))
    cross = SiteDirectory()
    cross.load_sites_from_lines(['{"site_id": 1, "site_name": "Only site", "site_url": "https://one.example.com"}'])
    report = cross.load_mailing_lists_from_lines(
        ['{"list_id": "team", "name": "Team", "members": ["a@example.com"], "site_ids": [1, 9999]}'])
    show("Cross-reference check (a list pointing at a site that does not exist)")
    for warning in report.warnings:
        print(f"    - {warning}")
    return users, incidents, directory


def oop_model(users, incidents):
    section(2, "The OOP model")
    admin = users.get("admin")
    task = next(i for i in incidents.list_all() if i.kind == MaintenanceTask.KIND)
    fault = next(i for i in incidents.list_all() if i.kind == "fault")
    show("__str__ ", str(task))
    show("__repr__", repr(fault))

    show("Polymorphism - one loop, no type checks: each incident supplies its own kind and fields")
    for incident in (task, fault):
        print(f"    {incident.kind:<12} {incident.extra_fields()}")

    show("Validation raises ValueError and leaves no invalid object behind")
    for description, attempt in [
        ("blank title", lambda: MaintenanceTask("   ", "d", admin)),
        ("site with an ftp:// address", lambda: Site(5000, "Files", "ftp://files.example.com")),
        ("weak password", lambda: User("newbie", Role.USER, "short")),
    ]:
        try:
            attempt()
        except ValueError as exc:
            print(f"    {description:<28} -> ValueError: {exc}")


def data_structures(users, incidents):
    section(3, "Data structures")
    admin = users.get("admin")
    maintenance = [i for i in incidents.list_all() if i.kind == MaintenanceTask.KIND]
    faults = [i for i in incidents.list_all() if i.kind == "fault"]

    show("deque - the maintenance queue is strict FIFO (arrival order is fairness)")
    queue = MaintenanceQueue()
    for task in maintenance[:3]:
        queue.enqueue(task)                                   # deque.append
    show("  pending", titles(queue))
    while True:
        try:
            started = queue.start_next()                      # deque.popleft
        except IndexError as exc:                             # empty queue handled, not a crash
            show("  queue empty", exc)
            break
        queue.complete_current(admin, "Done in the demo.")
        show("  started and completed", started.title)

    show("heapq - the fault queue pops the most severe first (1 = Critical), ties by arrival")
    fault_queue = FaultPriorityQueue()
    for fault in faults:
        fault_queue.push(fault)
    for _ in range(3):
        popped = fault_queue.pop_most_severe()
        show("  popped", f"{popped.severity.name:<9} {popped.title}")

    show("dict - lookup by id, and .get() where a missing key is expected")
    show("  incidents.get('seed-003')", incidents.get("seed-003").title)
    show("  users.get('nobody')", users.get("nobody"))

    counts = {}                                               # counting dict
    for fault in faults:
        counts[fault.severity.name] = counts.get(fault.severity.name, 0) + 1
    show("  faults per severity")
    for severity, count in counts.items():                   # .items() with unpacking
        print(f"    {severity:<9} {count}")

    show("set - which technicians have work assigned, and who is free")
    technicians = {u.username for u in users.all_users() if u.role == Role.USER}   # set comprehension
    busy = {i.assigned_to.username for i in incidents.list_all() if i.assigned_to}
    show("  technicians", sorted(technicians))
    show("  with assigned work", sorted(busy & technicians))   # intersection
    show("  without assigned work", sorted(technicians - busy))  # difference
    everyone = technicians | {"admin"}                        # union
    everyone.discard("nobody")                                # discard: no error when absent
    show("  technicians + admin", sorted(everyone))


def fault_sort_key(fault):
    """Sort key: severity first (1 = Critical), then title - a two-field tuple."""
    return fault.severity.value, fault.title


def sorting_and_comprehensions(incidents):
    section(4, "Sorting and comprehensions")
    faults = [i for i in incidents.list_all() if i.kind == "fault"]          # list comprehension
    ordered = sorted(faults, key=fault_sort_key)                              # named function as key
    most_severe, *the_rest = ordered                                          # * collects the remaining items
    show("Most severe fault (severity, then title)", f"{most_severe.severity.name} - {most_severe.title}")
    show("Faults left after it", len(the_rest))
    longest = sorted(faults, key=lambda f: len(f.title), reverse=True)[:3]    # lambda as key
    show("Three longest fault titles", titles(longest))
    by_id = {i.id: i.title for i in incidents.list_all()}                     # dict comprehension
    show("dict comprehension (id -> title)", f"{len(by_id)} entries, e.g. seed-001 -> {by_id['seed-001']!r}")
    creators = {i.created_by.username for i in incidents.list_all()}          # set comprehension
    show("set comprehension (who reported incidents)", sorted(creators))


def iterators(incidents):
    section(5, "Iterable and Iterator classes")
    queue = MaintenanceQueue()
    for task in [i for i in incidents.list_all() if i.kind == MaintenanceTask.KIND and i.status.value == "open"][:3]:
        queue.enqueue(task)
    first, second = iter(queue), iter(queue)                 # every iter() builds a NEW iterator
    show("iter(queue) returns", f"{type(first).__name__} - a separate Iterator class with __next__")
    show("first iterator, next()", next(first).title)
    show("first iterator, next()", next(first).title)
    show("second iterator, next()", f"{next(second).title}   <- independent: it starts from the beginning")
    show("first iterator, rest", titles(first))
    try:
        next(first)
    except StopIteration:
        show("first iterator, next() again", "StopIteration - it is exhausted")
    show("second iterator still has", f"{second.remaining} items left")


def watched(items):
    """Pass items through unchanged, printing each one the moment it is read - makes laziness visible."""
    for item in items:
        print(f"      ...reading {item.title!r}")
        yield item


def generator(users, incidents):
    section(6, "Generator with yield - work that may be stuck (In progress, no update for 4+ hours)")
    now = datetime.now(timezone.utc)
    work = [i for i in incidents.list_all() if i.kind == MaintenanceTask.KIND and i.status.value == "open"][3:8]
    hours_since_update = [1, 5, None, 9, 30]                 # None: never started (stays Open)
    for task, hours in zip(work, hours_since_update):
        if hours is not None:
            task.start_progress()
            task.updated_at = now - timedelta(hours=hours)
    gen = stale_in_progress_work(watched(work), now)
    show("1. Generator created - no '...reading' lines yet, so nothing was read")
    show("2. next()", next(gen).title)
    show("3. Continue with a for loop (4. it resumes after the item next() returned)")
    for incident in gen:
        print(f"    -> {incident.title}")
    show("5. Exhausted - a second loop yields", list(gen))
    show("   A NEW generator starts again", next(stale_in_progress_work(work, now)).title)


def lazy_pipeline(incidents):
    section(7, "Lazy pipeline - open maintenance calls waiting more than 3 days")
    total = sum(1 for _ in incidents.list_all())
    later = datetime.now(timezone.utc) + timedelta(days=4)  # pretend four days have passed
    calls, examined = first_pressing_calls(incidents.list_all(), limit=2, now=later)
    for call in calls:
        show("result", call["summary"])
    show("Stopped after 2 results", f"examined {examined} of {total} incidents - {total - examined} never touched")


def context_managers(users, incidents, directory):
    section(8, "Context managers")
    admin = users.get("admin")
    task, other = [i for i in incidents.list_all() if i.kind == MaintenanceTask.KIND and i.status.value == "open"][:2]
    with IncidentWorkSession(task, admin):
        pass                                                  # the work itself
    show("IncidentWorkSession, normal exit", task.comments[-1].text)
    try:
        with IncidentWorkSession(other, admin):
            raise RuntimeError("disk full")
    except RuntimeError as exc:
        show("IncidentWorkSession, exception", f"{other.comments[-1].text}  (exception still raised: {exc})")

    site = directory.get_site(1001)
    with SiteCheckSession(site):
        show("SiteCheckSession, inside", f"is_running(1001) = {SiteCheckSession.is_running(1001)}")
        try:
            with SiteCheckSession(site):
                pass
        except RuntimeError as exc:
            show("A second check of the same site", f"refused - {exc}")
    show("SiteCheckSession, after exit", f"is_running(1001) = {SiteCheckSession.is_running(1001)} (lock released)")


def sites_subsystem(users, incidents, directory):
    section(9, "Sites & Mailing Lists subsystem")
    admin = users.get("admin")
    demo_site = directory.create_site(admin, "Demo status page", DEMO_SITE_URL)
    show("Created", f"{demo_site.label} ({demo_site.site_name}) - no incident mentions it")

    for round_number in (1, 2):
        directory.check_all()
        watch = [directory.get_site(1007), demo_site, directory.get_site(1002)]
        show(f"Check round {round_number}", ", ".join(f"{s.label} {s.status.value}" for s in watch))
    show("(1 failed check -> Degraded, 2 in a row -> Down; Site 1002 answers normally)")

    drafts = [n for n in directory.notifications() if n.state.value == "draft"]
    show("Notification drafts waiting for a decision", len(drafts))
    hr_draft = next(n for n in drafts if n.site_id == 1007)
    show("Sending the Site 1007 draft to", ", ".join(hr_draft.list_ids))
    directory.send_notification(hr_draft.id, admin, message="The HR portal is down - we are on it.")
    show("State now", hr_draft.state.value)

    reporter = users.get("tech1")
    incidents.add(MaintenanceTask("Check Site #1007 login and Site 1099", "Reported by phone", reporter))
    scan = directory.scan_incidents(incidents.list_all())
    show("Incident scan - active incidents per site (by id or full name)",
         {site_id: len(ids) for site_id, ids in sorted(scan.by_site.items())})
    show("Mentions of sites that do not exist", sorted(scan.unknown))

    print()
    for line in build_site_report(directory, incidents.list_all()).lines():
        print(f"  {line}")


def main() -> None:
    print("Incident Bridge - Stage 1 demo (offline: no server, no database, no network)")
    users, incidents, directory = load_data()
    oop_model(users, incidents)
    data_structures(users, incidents)
    sorting_and_comprehensions(incidents)
    iterators(incidents)
    generator(users, incidents)
    lazy_pipeline(incidents)
    context_managers(users, incidents, directory)
    sites_subsystem(users, incidents, directory)
    print("\nDemo finished.")


if __name__ == "__main__":
    main()
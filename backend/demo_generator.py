"""
Demonstration of the generator function with `yield`
(Stage 1, Part D item 2) - run with:  python demo_generator.py

Walks through the five behaviours the instructions ask to show, using only
the domain layer (no HTTP, no database).
"""

from datetime import datetime, timedelta, timezone

from app.iterators import stale_in_progress_work
from app.models import MaintenanceTask, Role, User

user = User(username="tech1", role=Role.USER, password="Passw0rd1")
now = datetime.now(timezone.utc)


def work_item(title, hours_since_update, started=True):
    task = MaintenanceTask(title=title, description="demo", created_by=user)
    if started:
        task.start_progress()
    task.updated_at = now - timedelta(hours=hours_since_update)  # after start_progress(), which stamps "now"
    return task


incidents = [
    work_item("Fresh work (1h ago)", 1),
    work_item("Never started", 10, started=False),
    work_item("Stale: restart backup agent", 5),
    work_item("Stale: rotate certificates", 9),
    work_item("Stale: clean disk", 30),
]


def watched(items):
    """Prints each incident the moment the generator actually looks at it."""
    for item in items:
        print(f"      ...looking at {item.title!r}")
        yield item


print("1. Create the generator - nothing is processed yet")
gen = stale_in_progress_work(watched(incidents))
print("   created; no 'looking at' lines above means no data has been read.\n")

print("2. One read with next()")
first = next(gen)
print(f"   -> {first.title!r}   (stopped right after finding it; the rest were not looked at)\n")

print("3. Continue with a for loop")
for incident in gen:
    print(f"   -> {incident.title!r}")
print("   Note (point 4): the loop started AFTER the first item - the generator resumed where it stopped.\n")

print("5. The finished generator cannot be iterated again")
print(f"   for loop over it again yields: {list(gen)}")
try:
    next(gen)
except StopIteration:
    print("   next(gen) raises StopIteration - it is exhausted.")
again = stale_in_progress_work(incidents)
print(f"   A NEW generator starts from the beginning again: first result is {next(again).title!r}")

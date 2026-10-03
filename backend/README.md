# Incident Bridge — backend

Matches the confirmed design decisions in the project's main README (v0.3).

## What's included

```
backend/
├── app/
│   ├── models/
│   │   ├── enums.py             # Role, IncidentStatus, ResolutionType, SeverityCategory
│   │   ├── user.py              # User — password hashing + PasswordPolicy enforcement
│   │   ├── comment.py           # Comment — one entry in an incident's update thread
│   │   ├── incident.py          # Incident (abstract base) — shared lifecycle + close()
│   │   ├── maintenance_task.py  # MaintenanceTask(Incident)
│   │   └── fault.py             # Fault(Incident) — severity + admin-only change_severity()
│   ├── services/
│   │   ├── password_policy.py   # PasswordPolicy — min 8 chars, A-Z/a-z/0-9 only
│   │   └── severity_scoring.py  # SeverityScorer — pluggable placeholder rule set
│   ├── queues/
│   │   ├── maintenance_queue.py # MaintenanceQueue (deque, strict FIFO) + MaintenanceQueueManager
│   │   └── fault_queue.py       # FaultPriorityQueue (heapq, all severities share one queue)
│   ├── context.py               # IncidentWorkSession — context manager, audit-trail on exit/exception
│   ├── persistence.py           # SqliteIncidentStore — incidents + comments survive a restart
│   ├── repository.py            # UserStore (pre-provisioned users) + IncidentRepository (in-memory + persisted)
│   ├── events.py                # EventBroadcaster — live-update fan-out (SSE), threading + asyncio together
│   ├── state.py                 # AppState — loads persisted incidents and rebuilds both queues at startup
│   └── api/
│       ├── deps.py              # get_state / get_current_user / get_broadcaster / get_client_id
│       ├── schemas.py           # Pydantic request bodies
│       ├── serializers.py       # domain objects -> plain JSON-safe dicts
│       ├── auth.py              # /auth/login, /logout, /me, /change-password
│       ├── incidents.py         # /incidents/... — maintenance + fault endpoints, each one also broadcasts
│       ├── events.py            # GET /events — the Server-Sent Events stream
│       └── app.py               # FastAPI app: session/CORS middleware, routers, error mapping
├── demo.py                      # runnable walkthrough of the domain layer alone (no HTTP)
├── smoke_test_api.py            # runnable walkthrough of the full API over HTTP
├── serve_app.py                 # `python serve_app.py` - loads .env, then runs uvicorn
├── .env.example                 # required/optional environment variables, documented
└── requirements.txt
```

The frontend side of live updates lives in `frontend/js/`: `events.js` (SSE client + per-tab client id),
`toast.js` (notifications), and `row-list.js` (flicker-free row patching - see the Live updates section below).

Every enforced rule from the design doc is live in code, not just documented:

- FIFO order in `MaintenanceQueue` is genuinely un-reorderable — there is no method that could reorder it. Trying to start a second task while one is in progress returns HTTP 409.
- `FaultPriorityQueue` pops Critical before Major before Minor, always, regardless of insertion order — verified over real HTTP requests, not just in-process, and verified again across a process restart (see Persistence below).
- `Incident.close()` requires a non-empty message and raises `PermissionError` (→ HTTP 403) if a non-admin tries to set `NOT_AN_INCIDENT` or `BY_DESIGN`.
- `PasswordPolicy` is enforced inside `User.set_password()` itself, so the API layer can't accidentally skip it.
- `IncidentWorkSession` (a context manager) logs an audit-trail comment whether the work block succeeds or raises.
- No self-registration endpoint exists anywhere — users only come from `INCIDENT_BRIDGE_USERS` at startup, every time the process starts (this is still true with persistence added — see below).
- Permission logic lives in exactly one place (the domain layer) — the API layer never re-checks roles itself, it just lets the domain's `PermissionError`/`ValueError`/`RuntimeError` bubble up to a small set of exception handlers that map them to the right HTTP status.

## Persistence

Incidents and their comments now survive a process restart, via SQLite (`app/persistence.py`). **Users still do not persist** — that's a deliberate, explicitly-deferred future task, not an oversight; they're re-seeded from `INCIDENT_BRIDGE_USERS` every time the process starts, exactly as before.

- **Where**: one file, path set by `DATABASE_PATH` (default `backend/incident_bridge.db`, created automatically). Use `:memory:` for a throwaway database that never touches disk — that's what `smoke_test_api.py` does, so repeated test runs never see leftover data from a previous run.
- **What's stored**: every incident's full current state (title, description, status, resolution, severity, etc.) and every comment. `IncidentRepository.save()` is the one call site every mutating endpoint uses — it updates the in-memory copy and persists it in the same call, so there's no separate "remember to persist" step to forget.
- **Startup replay**: `AppState.create()` loads every persisted incident (ordered by creation time) and puts each one back exactly where it would be at runtime: an `open` maintenance task goes back into the pending FIFO deque, the one `in_progress` task (there's only ever one) is restored directly as the queue's current item via `MaintenanceQueue.restore_current()` — not replayed through `start_next()`, since that's for making a new scheduling decision, not replaying one already made — an `open` fault goes back into the priority heap, and anything `closed` (or an already-claimed, still-`in_progress` fault) just goes into the repository for lookup, matching how it already behaves at runtime.
- **Unknown users after a restart**: if `INCIDENT_BRIDGE_USERS` changes between restarts and a persisted incident references a username that's gone, `persistence.py` doesn't crash — it substitutes a clearly-labeled placeholder account (`"<name> (removed)"`) so old history stays readable instead of disappearing or blowing up startup.
- **Concurrency**: FastAPI runs sync endpoint functions in a thread pool, so more than one request can genuinely reach the database at the same time. One `sqlite3` connection is kept open for the process's life (required for `:memory:` to work at all — a fresh connection would just see an empty database) and every access is serialized with a `threading.Lock`.

## Live updates

Other users' changes now show up without a manual refresh, via Server-Sent Events (SSE) — not WebSockets, since the browser only ever needs one-way push here (mutations still go through the normal REST endpoints).

- **Backend** (`app/events.py`, `app/api/events.py`): `EventBroadcaster` holds one `asyncio.Queue` per connected browser tab, keyed by that tab's self-chosen `client_id`. Every mutating endpoint in `incidents.py` calls `broadcaster.publish(...)` right after `state.incidents.save(...)` succeeds. `GET /events?client_id=...` is the long-lived SSE stream each tab connects to, with a 20-second keepalive comment so an idle connection doesn't get silently dropped by a proxy in front of it (relevant on Render).
- **This is the second genuine use of `threading`, paired with `asyncio`**: route handlers are sync `def`s running in FastAPI's thread pool, but `publish()` needs to hand an event to `asyncio.Queue` instances owned by the event loop, from a different thread. `asyncio.Queue` isn't safe to write to directly from another thread, so `publish()` uses `loop.call_soon_threadsafe(...)` — the supported way to cross that boundary.
- **Self-exclusion**: every mutating request from the frontend sends an `X-Client-Id` header (a random id `events.js` generates per browser tab, stored in `sessionStorage` — not `localStorage`, specifically so two tabs of the *same logged-in user* count as separate subscribers). The broadcaster skips sending a tab's own change back to itself. Verified live with two real SSE connections under the same account: the acting tab receives nothing, the other tab receives exactly one event.
- **Dashboard** (`dashboard.js`): on any event, shows a toast (`"tech1 commented on Payment API is down"`, etc. — see `toast.js`'s `describeEvent()`) and re-fetches all three lists. Re-rendering goes through `row-list.js`, which patches existing row elements in place (matched by incident id) instead of rebuilding the DOM, so an unrelated list's rows are never touched and nothing flickers — the explicit no-flicker requirement.
- **Incident detail page** (`incident.js`): if an incoming event's id matches the incident currently being viewed, shows a blocking "This incident changed — refresh?" dialog. **Refresh** reloads the page. **Not now** disables every button/input on the page and shows a persistent banner ("Viewing outdated data") — the page is never silently re-rendered out from under whatever the user was doing, so nothing they'd typed (e.g. a half-written comment) is lost. Once declined, every mutating action stays locked until an actual page refresh, even if the user tries anyway (each handler checks the same `locked` flag as a second line of defense beyond the disabled attributes).

## API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/login` | body `{username, password}` |
| POST | `/auth/logout` | clears the session |
| GET | `/auth/me` | current user |
| POST | `/auth/change-password` | body `{current_password, new_password}` |
| GET | `/incidents` | optional `?type=maintenance\|fault&status=open\|in_progress\|closed` |
| GET | `/incidents/{id}` | detail view, either incident type |
| POST | `/incidents/{id}/comments` | body `{text}` — works on closed incidents too |
| GET | `/incidents/maintenance/queue` | `{current, pending}` |
| POST | `/incidents/maintenance` | body `{title, description}` |
| POST | `/incidents/maintenance/start-next` | 409 if one's already in progress |
| POST | `/incidents/maintenance/complete-current` | body `{message, resolution_type}` |
| GET | `/incidents/faults/queue` | priority order, all severities |
| POST | `/incidents/faults` | body `{title, description, details}` — severity is auto-scored |
| POST | `/incidents/faults/claim-next` | pops + assigns the most severe fault |
| PATCH | `/incidents/faults/{id}/severity` | body `{severity}` — admin-only; repositions the fault in the priority queue if it's still queued |
| POST | `/incidents/faults/{id}/close` | body `{resolution_type, message}` — faults only, not maintenance tasks |
| GET | `/events?client_id=...` | Server-Sent Events stream for live updates |
| GET | `/health` | plain liveness check |
| GET | `/docs` | interactive Swagger UI (FastAPI auto-generated) |

Note: maintenance tasks are only ever closed via `/incidents/maintenance/complete-current`, never the generic fault-close endpoint — that's what keeps the queue's "current task" pointer correctly in sync (see `app/queues/maintenance_queue.py`). Every mutating endpoint above also persists (`state.incidents.save(...)`) and broadcasts (`broadcaster.publish(...)`), in that order, right after the domain-level mutation succeeds.

## What's deliberately NOT here yet

- No automated test suite yet (Stage 3) — `demo.py` and `smoke_test_api.py` are manual walkthroughs standing in for one for now; `sse_test.py` was a one-off manual verification script for the live-update work and isn't kept around as a permanent fixture.
- No Stage 2 external service integration.
- No user persistence — see Persistence above; explicitly deferred.
- No dark/light theme toggle yet, and the account modal hasn't been renamed to "Settings" with an Interface tab yet — a separate, explicitly-future task.

## How to run

Requires Python 3.9+.

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # then edit it - see below for how it gets loaded
python serve_app.py
```

`serve_app.py` loads `.env` itself (via python-dotenv) before starting the app, so
`INCIDENT_BRIDGE_USERS`, `SESSION_SECRET_KEY`, `ALLOWED_ORIGINS`, `DATABASE_PATH`, and the
`HOST`/`PORT`/`RELOAD` settings in your `.env` all take effect automatically -
just running `python serve_app.py` picks them up.

If you use the plain `uvicorn` CLI instead, note that it does **not** load
`.env` on its own:
```bash
uvicorn app.api.app:app --reload
```
That works fine, but anything you put in `.env` will be silently ignored
unless you export those variables into your shell yourself first, or add
`--env-file .env` to the command.

Visit `http://127.0.0.1:8000/docs` (or whatever `PORT` you set) for the
interactive API docs.

To run the domain-layer walkthrough without any HTTP at all (no database involved):
```bash
python demo.py
```

To exercise the full API over HTTP (uses FastAPI's TestClient and an in-memory database, no server needed):
```bash
python smoke_test_api.py
```

To see live updates yourself: run `python serve_app.py`, open the dashboard in two
different browser tabs (or two different browsers) logged in as the same or different
users, and perform an action in one - a toast and an updated row should appear in the
other within about a second, with no flicker anywhere else on the page.

## Next step

Dark/light theme toggle and renaming the account modal to "Settings" with an Interface tab, then Stage 2 external service integration, Stage 3 automated tests, and Stage 4 concurrency/asyncio refinements — see the project's main README for how these are scoped.



## Lazy pipeline (`app/iterators.py`)

`pressing_maintenance_calls(incidents)` chains three generator expressions over the incident repository, with no intermediate lists:

1. keep only **open maintenance calls** (calls already in progress, and closed ones, are left out);
2. keep only calls **open for more than 3 days** (time since `created_at`), converting each to a tuple;
3. return a small record with the **id**, title, reporter, days open and a one-line **summary** such as `Patch server - open for 5 days (a1b2c3d4)`.

It is exposed at `GET /incidents/maintenance/pressing[?limit=N]` and shown on the dashboard's **Pressing Maintenance Calls** page (`/pressing.html`, opened from the "Pressing maintenance calls" button). The page refreshes on every live update, and each row links to its incident.

- **What makes the pipeline start working?** Nothing happens when it is built. Work begins only when a consumer asks for a value (`next()`, a `for` loop, `islice`). Each request pulls one incident through all three stages.
- **What did not need processing after stopping?** With `limit=2`, once the second pressing call is found the source is never read again. The `examined` field in the API response shows how many incidents were pulled; every incident after that point was never touched.
- **List comprehension vs. expression generator here:** a list comprehension would scan every incident and build a full list at each stage before returning anything. The generator expression produces one result at a time and can stop early, using no extra memory for intermediate results.
- **Why a new generator is needed to go again:** a generator keeps its position and is exhausted once consumed. Iterating again from the start requires calling `pressing_maintenance_calls(...)` to create a fresh one (shown in `tests/test_iterators.py`).




## Generator with `yield` (`app/iterators.py`)

`stale_in_progress_work(incidents)` is a generator function: it contains `yield`, so calling it returns a generator object instead of running the body. It hands back, one at a time, only the incidents (faults or maintenance tasks) that are **in progress** but have had **no update for more than 4 hours** (`STALE_AFTER`, based on `updated_at`, which every status change and comment refreshes) - work that may be stuck. Open and closed incidents are never yielded.

It uses only the domain layer, so the permission/HTTP rules elsewhere in this README do not apply to it, and it reads from `IncidentRepository.list_all()` like the rest of the app (single process, in-memory state). It is a different tool from the lazy pipeline above: the pipeline chains generator *expressions*, while this is a generator *function*, whose body can hold state and logic between yields.

Run the demonstration (domain layer only, no HTTP or database): `python demo_generator.py`. It shows, in order:

1. **Creating the generator processes nothing yet** - no incident is read until a value is requested.
2. **One read with `next()`** - the generator runs only as far as the first stale incident, then pauses.
3. **Continuing with a `for` loop** - the loop pulls the remaining stale incidents.
4. **It continues from where it stopped** - the loop does not repeat the incident `next()` already returned; local variables and the position in the data are kept between yields.
5. **A finished generator cannot be iterated again** - a second loop yields nothing and `next()` raises `StopIteration`. To go through the data again, call `stale_in_progress_work(...)` to create a new generator.

Tests: `tests/test_stale_work_generator.py` covers each of these five behaviours plus the business condition (including the exact 4-hour boundary and a custom threshold).

File-map additions for the README: `iterators.py` (lazy pipeline + stale-work generator), `demo_generator.py` (runnable walkthrough of the generator), `tests/test_stale_work_generator.py`.

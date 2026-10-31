# AI Usage

This file documents how AI tools were used in Incident Bridge, as required by Part G of the project brief ("responsible use of AI"). The full design and implementation are described in [`README.md`](README.md).

## How the project was built

Incident Bridge started as an idea and was **designed and built by the Project's team members**: the business idea and process, the domain model, the core code and every product decision are ours.

**AI was used as an assistant at almost every step**, to:

- reformat, rearrange and verify code we wrote, and review it against the requirements document;
- point out gaps between the code and the brief, and suggest how to close them;
- write follow-up documentation, code comments/docstrings and usage instructions for the code files;
- generate the synthetic sample data where needed as required by the project's requirements document and help check it.

Every AI suggestion was **run, tested and reviewed by us before it was kept** (the project has 1488 automated tests, run on every push since implementation). Responsibility for the code and the decisions stays with the team members, and we can explain every part of the business process behind it.

**Tool used:** Claude (Anthropic), in chat sessions.

---

## 1. Generating the sample data

### Files

| File | Records | Contents |
|---|---|---|
| `data/sample_data.jsonl` | 35 | Incidents: 15 maintenance tasks and 20 faults (6 of them closed), several of them mentioning org sites — reduced from the original set in v0.5.x by keeping a subset of the records |
| `data/sites.jsonl` | 12 | Org sites for the Sites & Mailing Lists subsystem |
| `data/mailing_lists.jsonl` | 6 | Mailing lists, each related to some of the sites |

One JSON object per line. No real personal data, passwords or real systems: usernames are the app's demo accounts, email addresses are `@example.com`, and site addresses are `example.com`/`example.org`/`example.net`/`iana.org` or the reserved `.invalid` domain (which never resolves, so those sites reliably show as Down when checked).

### Record structure we asked for

The fields and rules match the validation in our classes (`from_dict` constructors):

- **Incident** (`sample_data.jsonl`): `id` (unique text, e.g. `"seed-003"`), `kind` (`"maintenance"` or `"fault"`), `title` (non-blank, up to 200 characters), `description` (non-blank), `created_by` (an existing username: `admin`, `tech1`, `tech2`, `tech3`), optional `assigned_to`, and for faults a `details` object with only the known fields — `system_unavailable`, `security_breach`, `performance_degraded`, `cosmetic_only` (true/false) and `affected_users_percent` (0–100).
- **Site** (`sites.jsonl`): `site_id` (positive whole number, unique), `site_name`, `site_url` (http/https only), optional `site_publish_date` (`YYYY-MM-DD`, not in the future).
- **Mailing list** (`mailing_lists.jsonl`): `list_id` (lower-case slug, e.g. `"ops-team"`), `name`, `members` (email addresses), `site_ids` (ids that exist in `sites.jsonl`).

### Final prompts (summarized)

1. **Incidents:** "Generate synthetic incident records for an internal IT incident-management system as JSONL, one JSON object per line, with these fields and rules: *(the incident structure above)*. Mix maintenance tasks and faults of every severity, realistic IT titles and descriptions, unique ids, only the usernames admin, tech1, tech2 and tech3, and no real personal data, I want something that a tech-oriented guy can read and understand and analyze in order to provide a solution/workaround."
2. **Sites and mailing lists:** "Generate 12 org sites and 6 mailing lists as two JSONL files with these fields: *(the site and mailing-list structures above)*. Use only example.com / example.org / example.net / iana.org addresses, plus a few addresses under a reserved `.invalid` domain so some sites are reliably unreachable. Members must be synthetic `@example.com` addresses, every `site_ids` value must exist in the sites file, and include one list with no members."
3. **Follow-up (incidents):** "Update some of the incidents so their title or description mentions one of these sites, by its id (`Site 1007`) or its full name (`Internal HR portal`)."

### Problems found and how we fixed them

| Problem | Fix |
|---|---|
| The incidents were first split over two files (`sample_data_1.jsonl`, `sample_data_2.jsonl`), and the second reused the first one's ids | Merged into one file, `data/sample_data.jsonl`, the name the brief asks for |
| Some records used `tech2`/`tech3`, which were not provisioned, so they were skipped as "unknown user" | Added `tech2` and `tech3` to `backend/.env.example` |
| No incident mentioned a site, so the subsystem's incident scan and report had nothing to show | Added site mentions to several incidents (the follow-up prompt) |
| Our loader became stricter during the project (a required `id`, typed `details` with only known fields) | Re-checked every record against the new rules; all passed |

### How the data was verified

- **The app's own validation:** every file is loaded line by line (`json.loads`, then the class's `from_dict`), which rejects duplicate or missing ids, missing fields, invalid values and broken JSON, and reports each skipped record with its reason. All 35 incidents, 12 sites and 6 lists load with **0 skipped**.
- **Automated tests:** `test_the_bundled_sample_data_has_valid_details_and_ids` and `test_the_real_seed_files_load_without_any_problem` fail if any bundled record stops passing validation.
- **The demo:** `python main.py` loads all three files and prints what was loaded and what was skipped.

### How to import the data

**Incidents** (`data/sample_data.jsonl`) — any one of:
- **In the app:** log in as an admin → **Server settings** → **Import data** → Incidents → choose `data/sample_data.jsonl`.
- **From the command line, into a running server:** start the app (`cd backend && python serve_app.py`), then in a second terminal `cd backend && python seed_from_jsonl.py` (it reads `../data/sample_data.jsonl` by default, or pass another path). The new incidents appear live in every open browser.
- **Demo only (in memory):** `python main.py` from the repository root.

**Sites and mailing lists** (`data/sites.jsonl`, `data/mailing_lists.jsonl`) — in the app: **Server settings** → **Import data** → Sites, then Mailing lists (lists refer to sites, so sites first).

**Re-importing is always safe:** records whose id already exists are skipped, never overwritten.

---

## 2. Other significant AI use

AI was used throughout development as a reviewer and assistant: checking our code against the requirements document (we re-ran this check several times as the project grew), reformatting and reorganizing code, writing docstrings, comments and the README, writing and extending automated tests, and verifying changes by running the test suite and the app (including browser checks of the frontend).

### Decisions affected by AI answers

The final decision was always the team's; these are the places where an AI answer or suggestion shaped it:

| Topic | What the AI pointed out or suggested | What we decided |
|---|---|---|
| Group-of-four extension | Our idea (org sites + availability checks) alone missed some requirements; suggested adding mailing lists, a notification process with states, the incident scan as the link to incidents, and a report | Built the Sites & Mailing Lists subsystem with those parts |
| Site availability | Compared a real ping, a third-party uptime service and our own HTTP check | Our own HTTP check behind an interface (`AvailabilityChecker`); one failure = Degraded, two in a row = Down |
| Notifications | Suggested drafting instead of sending automatically, so a flapping site cannot spam the lists | Drafts that an admin sends or dismisses; a newer draft supersedes an older one |
| Portal access | Showed that hiding the button is not enough | Admin-only enforced in the backend too, including live updates (role-based audience) |
| Deleting sites | Suggested never reusing site ids, so old incident text keeps pointing at the right site | Archive and restore instead of delete; ids never reused |
| Incident scan | Explained the matching options and edge cases (e.g. `Site 10070` vs `Site 1007`, overlapping names) | `Site <id>` with `#`/`-`, or the exact full site name; the longest name wins |
| Seed import of sites | Explained why commented-out settings still imported the default files | Made it opt-in: only when the setting is present |
| User provisioning | Pointed out that duplicate usernames were silently overwritten | The app refuses to start on duplicates (case-insensitive) and lists every problem |
| Data validation | Found that badly typed fault details crashed the whole import, and that records without an id were given random ids | Typed validation of details; records without an id are rejected |
| Polymorphism | Pointed out `isinstance` checks deciding behavior, which the brief forbids | Abstract `kind` and `extra_fields()` on `Incident`, used everywhere instead |
| Iterators | Explained why the queues needed a separate Iterator class, and why a "next/previous" preview popup would not count as one | `FifoQueueIterator` / `SeverityOrderIterator` working on a snapshot |
| Demo | Explained why the running web app cannot replace `main.py` | Wrote `main.py` as an offline walk-through of every requirement |
| Merging a teammate's branch | Found that a merge silently dropped existing features (the import dialog, queue labels, maintenance API calls) | Restored them before merging |
| Server shutdown | Found that stopping the server hung while browser tabs were open | Added a short graceful-shutdown timeout |
| Version number | Explained that setuptools cannot read a version file outside `backend/` | The version lives only in `backend/pyproject.toml` |

---

## 3. Stage 2 (brief §7.3)

<!-- Maintained by Person C. A and B: send C what you asked, what the tool suggested, what you checked
     and what you changed or kept. Every "TODO(C)" must be resolved before the final 0.6.0 tag. -->

### How we worked

For the Stage 2 tasks we followed the brief's rule: **first ask the AI for a plan and test cases only, without code changes**; each of us wrote the expected results of at least two test cases ourselves, from the business rule and not by running the code under test; then the code was written, checked against the existing test suite, and we never changed an expected result just to make a test pass.

**v0.5.0 is the exception, and we say so:** the move from the providers' SDKs to our own `requests`-based client (`backend/app/api_client.py`, `app/ai/providers/http_provider.py` and their tests) was written with AI help **before** we started asking for a plan first. It was reviewed and run (the whole suite passed), but the plan-first steps above were not followed for it.

### Person A — AI findings → Sites subsystem → report

<!-- TODO(C): from Person A - what was asked, what the tool suggested, the two expected results A wrote, what was checked, what was changed or kept -->

### Person B — TDD archive rule, async, threads + Lock

<!-- TODO(C): from Person B - same points; the TDD cycle itself is documented by the Red/Green/Refactor commits (README, שלב 2) -->

### Person C (Roi) — Stage 2 demo, documentation, integration

| Topic | What the AI pointed out or suggested | What we checked | What we decided |
|---|---|---|---|
| Plan | Read the Stage 2 brief and our team plan, listed Part C's tasks, and pointed out that the brief puts `pyproject.toml` and `.env.example` in the repository root, while ours are in `backend/` | The brief, sections 2.3 and 7.1 | Asked the lecturer (question 1) instead of moving files on our own; a move, if needed, is the last merge |
| Starting point | Noticed that the uploaded branch was v0.4.9 and contained only `backend/`, not v0.5.0 with the root files | `git log origin/feature/Gemini_API`, the version in `backend/pyproject.toml` | Fast-forwarded our branch to v0.5.0 before writing anything |
| `main.py --live` | Suggested `--live` calls only the configured AI provider, and stops with a clear message when no real provider is configured | That `.env` keys are never printed; the brief: no simulated data shown as if it came from the network | Kept: no silent fallback; every Stage 2 result is tagged SIMULATED or REAL; site checks stay simulated (the sample sites are not real) |
| Demo content | Suggested showing the same steps the worker runs (prompt → `analyze()` → comments), a service failure that changes nothing but the error comment, and the patterns | Ran `main.py` offline and the `--live` error cases | Kept; sections for Persons A and B print "pending" until their code is merged |
| Tests for the demo | Suggested extending `tests/test_main_demo.py` | Our own expected results: <!-- TODO(C): Roi - confirm these are the two you wrote --> (1) an offline run prints SIMULATED and never `[REAL]`; (2) `--live` with `GEMINI_API_KEY` empty exits with an error, prints the reason and nothing else | Kept both |
| Verification limits | The AI's sandbox could not install FastAPI/pytest (no network), so it ran `main.py` and the two demo tests directly, not the whole suite | Ran the full suite locally: <!-- TODO(C): Roi - paste the pytest summary line --> | — |

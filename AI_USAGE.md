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

### How we worked

For the Stage 2 tasks we followed the brief's rule: **first ask the AI for a plan and test cases only, without code changes**; each of us wrote the expected results of at least two test cases ourselves, from the business rule and not by running the code under test; then the code was written, checked against the existing test suite, and we never changed an expected result just to make a test pass.

**v0.5.0 is the exception, and we say so:** the move from the providers' SDKs to our own `requests`-based client (`backend/app/api_client.py`, `app/ai/providers/http_provider.py` and their tests) was written with AI help **before** we started asking for a plan first. It was reviewed and run (the whole suite passed), but the plan-first steps above were not followed for it.

### What the entries below cover

Team members also used AI tools on their own while writing their parts; those conversations were not kept. The entries below come from the team's **review and integration sessions** (Claude), where every Stage 2 part was checked against the brief before and after it was merged, and where the fixes listed here were made. Each entry says what the AI pointed out or suggested, what we checked ourselves, and what we decided.

### AI findings → Sites subsystem → report

| Topic | What the AI pointed out or suggested | What we checked | What we decided |
|---|---|---|---|
| Fit with the brief | Checked the findings block, the Observer listeners and the report metric against brief 6.1–6.4 (an answer that drives a decision, a new metric, the four extra tests) | The four tests in `test_integration.py` / `test_reports.py` and their expected results: a failed call or an answer without findings touches no site; an unknown or archived site id changes nothing; the metric lists exactly the AI-named sites that are Operational or have no sent notification | Kept the design: the measured check decides the site's status, never the AI |
| Encrypted database | Found that the request history opened the database with plain `sqlite3`, which cannot read the encrypted database, and that one test lacked the demo users | The history read and written through the app's own encrypted connection; the full suite | Fixed (`312fca3`) |
| Checks started by the AI | Pointed out that site checks triggered by the findings were not published, so they were missing from the activity log and from open site-portal pages | A check started by the AI appears like one an admin started (`site_checked`, by `ai-assistant`) | Fixed (`6d32ecd`) |
| Visible in the app | We asked what of Stage 2 an admin can actually see in the web app; it found the requests and their archive were not shown anywhere and suggested a view for them | The site portal's AI requests and Archive tabs, the per-site AI numbers, in a browser | Added the AI requests view (`5ff15c0`) |

### TDD archive rule, async collection, the claim Lock

| Topic | What the AI pointed out or suggested | What we checked | What we decided |
|---|---|---|---|
| Fit with the brief | Checked that the branch covers brief 4.2 (TDD), 5.1 (async) and 5.2 (threads + Lock), and that the Red / Green / Refactor commits stay visible | The commit history (`71ee90f`, `4e9bff6`, `72b7b29`); the expected results in `test_archive_rules.py` follow the business rule: a request that finished at least `AI_REQUEST_ARCHIVE_DAYS` ago is archived, an unfinished one never is, `0` means never | Merged with "Create a merge commit", never squashed, so the TDD history is kept |
| Using the rule | Pointed out that the archive rule was tested but not yet used by the app | Archived requests leave the site report's counts and are counted apart | Wired in (`844d64b`) |
| Async and the Lock | Suggested demo sections that show the difference instead of only describing it | `main.py` sections 14–15: concurrent collection takes about the longest delay, sequential the sum; of three simultaneous claims exactly one is approved; the tests race eight threads over many rounds and show the race without the lock | Kept |

### Integration, demo and documentation

| Topic | What the AI pointed out or suggested | What we checked | What we decided |
|---|---|---|---|
| Plan | Read the Stage 2 brief and our team plan, listed the integration tasks, and pointed out that the brief puts `pyproject.toml` and `.env.example` in the repository root, while ours are in `backend/` | The brief, sections 2.3 and 7.1 | Asked the lecturer instead of moving files on our own; the lecturer approved our layout |
| Starting point | Noticed that the uploaded branch was v0.4.9 and contained only `backend/`, not v0.5.0 with the root files | `git log origin/feature/Gemini_API`, the version in `backend/pyproject.toml` | Fast-forwarded our branch to v0.5.0 before writing anything |
| `main.py --live` | Suggested `--live` calls only the configured AI provider, and stops with a clear message when no real provider is configured | That `.env` keys are never printed; the brief: no simulated data shown as if it came from the network | Kept: no silent fallback; every Stage 2 result is tagged SIMULATED or REAL; site checks stay simulated (the sample sites are not real) |
| Demo content | Suggested showing the same steps the worker runs (prompt → `analyze()` → comments), a service failure that changes nothing but the error comment, and the patterns | Ran `main.py` offline and the `--live` error cases | Kept; the Stage 2 sections print "pending" until the code they show is merged |
| Tests for the demo | Suggested extending `tests/test_main_demo.py` | Our own expected results: (1) an offline run prints SIMULATED and never `[REAL]`; (2) `--live` with `GEMINI_API_KEY` empty exits with an error, prints the reason and nothing else | Kept both |
| Verification limits | The AI's sandbox could not install FastAPI/pytest (no network), so it ran `main.py` and the two demo tests directly, not the whole suite | Ran the full suite locally: `1488 passed` | — |
| Merging the branches | Traced a drop in the test count to a stray branch and stale counts in the README, and found two seed-script password tests lost when commits were combined | `git log` of each branch; the test count after every merge | Restored the two tests (`9dbcdb2`); the stray branch was never merged |
| Windows | Found that the activity-log file stayed open after the app stopped, and that two `serve_app.py` tests read the developer's real `.env` | Ran the suite on Windows | Fixed both (`bea5f9e`, `8093c32`) |
| Settings and security | We decided to keep every setting in the app and to drop the requirements demo page (it ran code from inside the app, and the brief does not ask for it); the AI planned the move and pointed out that the brief (§2.3) still needs the API key to be readable from the environment | A fresh setup and a restart with an edited `.env` (ignored); the key's fallback to `.env` | Settings are copied into the encrypted database at setup; an API key not saved in the app is still read from `.env`; the demo page removed (`c1930c5`) |
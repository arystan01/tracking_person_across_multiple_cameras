# TRACE dashboard

Streamlit operator interface for a control API. The current runnable backend is a local simulator. No real perception/routing/media API is connected.

## Run

Python 3.10+; tested with Python 3.12 and Streamlit 1.65.

```sh
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

In this checkout you can also use `../.venv/bin/python` from the parent prototype folder. Sign in with `operator` / `trace-demo`. This fixed demo login is not production authentication.

Choose **Create demo case**, or fill out **New case**. Creation starts preparation immediately. No source selector, manual start/end window, timeout input, or separate Start search button is needed.

The simulator advances when polled: PREPARING initially, CATCHING_UP after 2 seconds, LIVE_TRACKING after 30 seconds, WAITING after 60 seconds. It stays open until the operator chooses Close search; there is no automatic demo cutoff. These accelerated fixture timings are unrelated to real routing's limits. The app polls every 2 seconds while the current case is open. Reopening a case advances the demo to its elapsed position; the demo is not a background worker.

## Agreed state semantics

| lifecycle | mode | done | Display |
|---|---|---|---|
| PREPARING | CATCHING_UP (demo default) | false | Preparing search |
| SEARCHING | CATCHING_UP | false | Searching recordings |
| SEARCHING | LIVE_TRACKING | false | Live tracking |
| SEARCHING | WAITING | false | Waiting for sightings |
| CLOSED | PAST_SUMMARY | true | Search ended |
| FAILED | supplied by backend | false | Search failed |

`WAITING` is not completion. `stop_reason` explains completion: `ALL_ENDED`, `MAX_GAP`, `BUDGET_SPENT`, or `OPERATOR_CLOSED`. `errors[]` and `unobserved_windows` are displayed separately and never converted into a case-wide failure by the UI.

The operator supplies only `last_known.camera_id` and `last_known.time`, plus title, photo, and notes. The backend sets `created_at`, `window.start = last_known.time`, and `window.end = created_at`. The recording window and revision fields remain internal; operator screens show formatted case information. Live sightings may occur after its end. The frontend does not calculate camera travel times, rank observations, choose video intervals, or merge clips.

## Decisions and updates

Only **CONFIRM** and **REJECT** are available. The demo accepts a decision, persists a pending state, and simulates a routing job completing after 3 seconds. Search and candidate arrival continue during that interval. On completion, the backend updates the route, decision/clip state, and route revision. `LONG_GAP` observations remain excluded from the demo route until confirmation.

The UI refreshes snapshots on either `revision` or `route_revision`, ignores older snapshots, and keeps a decision pending until that candidate explicitly reports `APPLIED`. An unrelated revision does not complete a decision. Same-decision retries are idempotent in the demo. Contradictory decisions are rejected, including while pending. No undo or separate recalculation action is provided.

Reports are downloadable at any time and may contain unresolved observations. Results persist automatically. **Close search** ends the active case with `OPERATOR_CLOSED`; it does not require all observations to be reviewed. The local demo waits for pending decisions before allowing close. **Delete case** remains available for ended/failed local cases with explicit confirmation.

## Integration boundary

`search_dashboard/backend.py` defines the `ControlAPI` protocol and isolated `DemoControlAPI`. Replace the app's API factory with a client implementing this interface. It can call an in-process controller in a modular monolith; it does not require a separate HTTP service. No undocumented endpoint URLs are assumed.

`domain.py` defines the normalized CaseView used by the UI. `adapters.py` translates/validates future wire data. `service.py` only formats status and chooses newer snapshots. `views.py` draws the route received from the backend. `storage.py` is local simulator persistence, not a second production source of truth.

Detailed handoff: [Control API integration](docs/control_api.md).

Demo videos use the supplied per-camera animations. For real data the UI consumes `clip_url` and `merged_clip_url`; it never invokes FFmpeg. Imported candidate batches and CSV parsing from the previous prototype are no longer operator-facing features. Existing example files remain as historical fixtures and are not inputs to the new UI.

## Existing data

Old cases are loaded as read-only historical snapshots. Their photos and decisions remain intact; no automatic expiration or data deletion runs. The old code did not store a backend route, so no route is invented during migration. Legacy timeout/save flags are not part of new cases. The original database rows are unchanged on read.

Default local database: `data/trace.sqlite3`. Use `TRACE_DB_PATH` to override it. This folder may sync through iCloud. Local deletion retains a minimal receipt. The local lock protects concurrent mutations within one Streamlit process; production concurrency, access checks, and idempotency belong to the control API.

## Checks

```sh
python -m unittest discover -s tests -v
```

Tests cover server-selected timestamps, all mode transitions, incremental/live observations, pending review across unrelated updates/restarts, immutable decisions and concurrency, LONG_GAP gating, partial errors, terminal reasons, legacy snapshots, safe exports, deletion, and the UI flow.

Download report exports a readable PDF snapshot with case status, route sightings, candidate review decisions, and coverage issues. Times are shown in KST. It does not close the search.

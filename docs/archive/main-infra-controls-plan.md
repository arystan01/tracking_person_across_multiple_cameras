# Main Infra and Controls implementation plan

This archived implementation record led to the current contour. For the
authoritative current state use [`../architecture.md`](../architecture.md); for
remaining work use [`../deployment.md`](../deployment.md) and
[`../roadmap.md`](../roadmap.md).

Status: **internal contour implemented**. Main Infra and Controls run end to end
with contract-faithful fakes. Replacing a fake requires an adapter that
implements the existing port; it does not require changing Case Manager,
Routing, Controls, or Dashboard projections. The remaining work is real
component integration and deployment-environment validation.

Current phase status:

| Phase | Status | Evidence |
|---|---|---|
| 1 - shared boundary package | DONE | `javes_contracts`, deterministic JSON Schema generation, stale-version tests |
| 2 - application skeleton/in-memory ports | DONE | `javes_main/application`, `ports`, `adapters`, `test_main_infra.py` |
| 3 - Controls API and IAM | DONE internally | FastAPI, Argon2id login, bearer sessions, RBAC, no signup, idempotent mutations |
| 4 - Case Store/migrations | DONE internally | SQLAlchemy repository, Alembic 0001-0006, CAS, restart tests, immutable history |
| 5 - leases/outbox/scheduler | DONE internally | durable jobs, leases, deadlines, retries, cancellation, outbox and Routing triggers |
| 6 - Routing/artifacts | DONE internally | pure Routing adapter, CAS ticks, deterministic SVG persistence, CaseView URLs |
| 7 - operator/Clip flow | DONE with fake | HOLD, early/late confirm/release, ordered export, crash replay |
| 8 - close/delete/retention | DONE with real core | 14-day TTL, permanent audit, legal holds, physical local-file deletion |
| 9 - hardening | DONE for fake stack | HTTP demo, SQL restart, concurrency, metrics, schemas and package tests |

Not claimed by this status: real-video correctness, real ffmpeg output,
production retention compliance, a real Site Config service, a real Dashboard,
or a deployed PostgreSQL/persistent-filesystem environment. Those are the explicitly
remaining attachment/deployment tasks.

## Target outcome

After this plan is implemented, a complete search must run with the checked-in
fakes. Replacing a fake with a real worker must require only an adapter and
contract conformance tests. Dashboard, external CCTV access, real Site Config,
Perception/Clip cleanup handlers, and deployment remain external plug-ins rather
than reasons to rewrite the Case Manager.

Recommended first deployment shape:

```text
Dashboard
   |
   v
Controls / FastAPI
   |
   v
Application services / Case Manager
   |------- Case Store + audit + outbox
   |------- ArtifactStore port
   |------- SiteConfig port
   |------- Routing port -------- current routing package
   |------- Perception port ----- fake, then real adapter
   |------- Clip port ----------- fake, then real adapter
   `------- Retention port ------ fake, then real adapter
```

Implement this as a modular monolith first. Modules have explicit ports and can
later move into separate processes. Worker-specific schemas must not leak into
Dashboard projections or unrelated database consumers.

## Technology baseline

- Python 3.11+ and Pydantic v2.
- FastAPI for Controls/API.
- SQLAlchemy 2 and Alembic.
- PostgreSQL for deployed concurrency; SQLite may be used for unit/integration
  tests if all PostgreSQL-specific behaviour also has a PostgreSQL test lane.
- Database outbox plus leased job rows for the MVP. Do not require Redis,
  RabbitMQ, or Kafka before measured load justifies them.
- `ArtifactStore` abstraction backed by private `FileArtifactStore` in every
  environment; no separate cloud/object storage is planned.
- Injected `Clock` and ID generator in application services. No hidden
  `datetime.now()` in decision logic.

Suggested package layout:

```text
javes_main/
  api/             # FastAPI routes, auth context, HTTP DTOs
  application/     # commands, queries, Case Manager orchestration
  domain/          # case/job/clip lifecycle rules
  ports/           # worker, repository, clock, site, artifact protocols
  adapters/        # routing, fake/real workers, artifact and site adapters
  persistence/     # SQLAlchemy models, repositories, migrations
  projections/     # CaseView builder
  scheduler/       # heartbeat, leases, outbox and retry loops
```

## State machines

Case lifecycle is separate from Routing mode:

```text
CREATED -> SEARCHING -> CLOSED
                   \-> EXPORTING -> SAVED
CREATED/SEARCHING/CLOSED/SAVED -> DELETE_REQUESTED -> DELETED
```

Routing mode remains:

```text
CATCHING_UP | LIVE_TRACKING | WAITING | PAST_SUMMARY
```

Job lifecycle:

```text
PENDING -> LEASED -> DONE
                  -> FAILED -> PENDING retry
                  -> TIMEOUT -> PENDING retry
                  -> UNAVAILABLE
```

An OPEN Perception job is a subscription and is not part of a finite "wait for
all jobs" barrier. Case Manager invokes Routing after each accepted result,
operator action, or heartbeat.

## Persistence model

Use JSONB for versioned worker payloads and Routing's opaque state, plus indexed
columns required for lifecycle/concurrency queries.

Minimum tables:

1. `cases`
   - `case_id`, lifecycle status, operator/owner, target, profile;
   - pinned `site_id`, `site_version`, `params_version`, calibration version;
   - window and last-known metadata;
   - current `case_revision`, `route_revision`, created/closed/deleted times.
2. `routing_states`
   - `case_id`, state revision, opaque JSON, Routing contract version;
   - one active row or immutable revisions, selected explicitly.
3. `routing_runs`
   - trigger id/type, input revision, output revision, started/finished/error;
   - result fingerprint for replay/debugging.
4. `probe_requests`
   - canonical request JSON, status, retry root/count, priority, OPEN flag.
5. `jobs`
   - job id/type, payload/version, state, attempt, lease owner/expiry,
     deadline, dedupe key, result reference.
6. `probe_results`
   - unique result fingerprint, raw versioned payload, accepted timestamp.
7. `tracklets` and `candidates`
   - canonical identifiers and query/display metadata;
   - large crops/embeddings remain artifact references.
8. `operator_decisions`
   - immutable decision id, target ids, action, operator, time, optional revoke.
9. `route_revisions`
   - Routing output JSON, best hypothesis, mode, done/stop reason.
10. `artifacts`
    - stable ref, kind, MIME type, checksum, size, retention class, TTL.
11. `route_maps`
    - route revision, floor, artifact id, dimensions, alt text.
12. `clip_records`
    - external clip id, processing status, lifecycle state, revision, artifact.
13. `worker_commands`
    - Clip/Retention command id, payload version, state and response.
14. `case_events`
    - permanent append-only audit event id/type/actor/time/details; retention
      never rewrites or deletes it.
15. `outbox`
    - transactionally created integration work awaiting dispatch.

Unique constraints must enforce request/result/command idempotency. Foreign keys
use `case_id`, but blobs and internal Routing visits do not need to be normalized
into database tables unless a real query requires it.

## Public Controls API v1

Initial endpoints:

```text
POST /v1/cases
GET  /v1/cases
GET  /v1/cases/{case_id}
GET  /v1/cases/{case_id}/view
GET  /v1/cases/{case_id}/events
POST /v1/cases/{case_id}/decisions
POST /v1/cases/{case_id}/commands/close
POST /v1/cases/{case_id}/commands/export
POST /v1/cases/{case_id}/commands/delete
GET  /v1/cases/{case_id}/routes/{revision}/{floor_id}.svg
GET  /v1/cases/{case_id}/clips
GET  /v1/cases/{case_id}/live
GET  /health/live
GET  /health/ready
```

All mutating endpoints require an idempotency key and authenticated actor
context. Authorization is enforced in Controls before application commands.
The current internal IAM uses pre-created accounts and explicit roles; an
external identity provider can replace that boundary later without granting
unconditional access in handlers.

Worker transport is adapter-specific. The application port should express
`submit`, `cancel`, and result ingestion; it must not assume HTTP or a particular
queue. If an HTTP polling transport is used initially, expose it under a
separate worker-authenticated surface.

## Transaction boundaries

### Create case

One transaction:

1. validate idempotency key and CaseRequest;
2. resolve and pin an immutable site/params version;
3. create the case and audit event;
4. initialise Routing state;
5. persist initial probes/jobs and outbox entries;
6. commit once.

Reference image persistence and target-gallery creation may be asynchronous, but
the case must expose a clear `CREATED/PREPARING` state until the gallery exists.

### Accept worker result

One transaction records the raw result and its unique fingerprint. It then
creates a unique Routing trigger. Duplicate delivery returns the previously
accepted result and does not create another trigger.

### Routing tick

1. Load case, pinned config, opaque state revision N, unprocessed accepted
   results, and operator anchors.
2. Call pure Routing outside a long database transaction.
3. Render SVG with the pinned floor assets.
4. Atomically compare-and-swap revision N to N+1.
5. In the same commit, persist route revision, artifacts metadata, probes/jobs,
   clip intents, audit event, and outbox rows.
6. On revision conflict, discard staged artifacts or mark them orphaned and
   retry from the new state.

This prevents two simultaneous Perception results from overwriting each other.

### Operator commands

- Confirm/reject creates an immutable decision and a Routing trigger.
- Close stops new search work, cancels OPEN jobs, calls Routing with
  `operator_closed`, and emits `CLOSE_CASE` retention intent.
- Export is a separate command after close and sends an ordered confirmed clip
  list. Repeating close never downgrades a saved export.
- Delete marks `DELETE_REQUESTED`, revokes active access, and emits a Retention
  command. Only successful retention completion moves the case to `DELETED`.

## Case Manager cadence

Case Manager reacts to:

- case creation/gallery readiness;
- each accepted finite or streaming Perception result;
- operator confirm/reject/revoke/close;
- heartbeat every Routing `heartbeat_s` while active;
- job lease expiration/deadline;
- Clip and Retention completion.

Do not implement a permanent barrier waiting for every probe in a round. A
finite batch may have a coordination deadline, but DONE results are useful as
soon as they arrive, FAILED/TIMEOUT are observations about execution rather than
negative detections, and OPEN jobs remain active.

## CaseView projection

Build `CaseView` only from persisted accepted state. It should contain:

- case lifecycle and Routing mode as separate fields;
- route revision and ready per-floor SVG URLs/inline payloads;
- candidates and review state;
- best hypothesis, alternatives, rejected evidence, and estimate;
- probe/search progress;
- live stream reference and watch cameras;
- clip processing/lifecycle status and export status;
- unobserved windows and user-actionable errors.

Dashboard must not parse opaque `SearchState`, recompute paths, infer modes, or
construct clip order.

## Implementation phases

### Phase 1 - shared boundary package

- Extract/freeze public DTOs from Routing and `javes_fakes/contracts.py` into a
  small versioned package or generated JSON Schemas.
- Keep Routing-internal state models private.
- Generate canonical examples and reject incompatible contract versions.

Acceptance: all checked-in JSON and fake outputs validate from a clean install;
Routing tests remain green.

### Phase 2 - application skeleton and in-memory ports

- Create `javes_main` layout, settings, clock, ID provider, ports, domain enums,
  and application error model.
- Implement in-memory repositories and `ArtifactStore`.
- Wire internal IAM, fake workers, and current Routing adapter through dependency
  injection.

Acceptance: application services run without FastAPI or a database and no
service imports a concrete fake directly.

### Phase 3 - Controls API

- Implement the public endpoints above.
- Validate idempotency, actor, lifecycle transitions, and problem-detail errors.
- Consume `examples/dashboard_requests.json` in API tests.

Acceptance: the entire fake Dashboard request sequence has stable response
schemas and replaying a mutating request is safe.

### Phase 4 - Case Store and migrations

- Add SQLAlchemy repositories and Alembic migrations for the minimum tables.
- Implement optimistic revisions, unique dedupe constraints, audit append, and
  artifact metadata.
- Run repository tests on PostgreSQL in CI; retain fast SQLite tests if useful.

Acceptance: process restart preserves a case; conflicting state updates cannot
silently overwrite one another.

### Phase 5 - jobs, leases, outbox, and scheduler

- Implement job claiming, lease renewal, deadline, retry, cancellation, and
  outbox dispatch.
- Add heartbeat triggers without duplicate accumulation.
- Treat OPEN jobs as subscriptions.

Acceptance: crash after commit/before dispatch recovers; duplicate results,
expired leases, timeout, unavailable video, and OPEN cancellation are tested.

### Phase 6 - Routing orchestration and artifacts

- Integrate `init_state()` and `step_and_render()` through `RoutingPort`.
- Pin site/params/calibration versions.
- Implement CAS Routing ticks and deterministic trigger/result consumption.
- Store route JSON and per-floor SVG using `ArtifactStore`.

Acceptance: fake Perception drives the current Routing across multiple rounds,
and `GET .../view` exposes the new SVG revision without Dashboard geometry.

### Phase 7 - candidate, Clip, and operator flow

- Create HOLD intents for evidence in surviving hypotheses according to the
  agreed policy.
- Confirm/reject updates Routing anchors and sends idempotent Clip commands.
- Export confirmed clips in Routing visit order.

Acceptance: confirm, reject, repeated decisions, release, missing source, and
ordered export pass against `FakeClipWorker`.

### Phase 8 - close/delete/retention flow

- Separate close, export/save, and delete commands.
- Register artifact retention classes and TTLs.
- Dispatch retention commands and wait for completion before final deletion.

Acceptance: `FakeRetentionWorker` deletes only policy-selected resources,
preserves legal holds, and duplicate commands are harmless.

### Phase 9 - full fake end-to-end and hardening

- Run the standard scenario from HTTP through DB, jobs, fake Perception,
  Routing, SVG storage, CaseView, review, fake Clip, export, close, and fake
  Retention.
- Test concurrent results, process restart, malformed payload, stale contract,
  stale revision, worker outage, partial coverage, and audit completeness.
- Add metrics for active cases, queue age, job latency, Routing latency,
  camera-seconds, result duplicates, retries, and unobserved windows.

Acceptance: one documented command starts the stack and the complete scenario
passes repeatedly with byte-stable contract outputs where determinism applies.

## Components attached after Main Infra is complete

### Real Dashboard

Replace fake HTTP requests with the UI. The Dashboard only needs Controls API,
CaseView, route SVG, live stream URL, decisions, and case commands.

### Real Perception and Clip

Implement adapters and run the handoff checklist in
`../reference/perception-clip.md`. Do not change domain/application services to
fit a worker-specific payload; translate it at the adapter boundary.

### Video Archive

The existing CCTV archive is external and read-only. Perception and Clip own
their access and translate source time/coverage into JAVES-AI contracts. Main
Infra, Routing and Retention neither operate it nor delete `archive_id`.

### Site Config

Provide immutable versioned graph and floor assets. Main Infra validates and
pins a version at case creation, then supplies graph data and asset bytes to
Routing. A config change affects new cases unless explicit migration is
designed.

### Real Retention

The durable Retention core and local `FileArtifactStore` deletion are now
implemented. Attach real Perception/gallery and Clip-output cleanup handlers;
keep case results for 14 calendar days and complete audit events forever.

### Deployment

Package API/Case Manager and scheduler/worker processes, PostgreSQL, artifact
storage, migrations, health checks, secrets, backups, observability, and worker
connectivity. Docker Compose is sufficient for local integration; production
orchestration is a deployment decision, not a domain contract.

## Definition of infrastructure-ready

Main Infra and Controls are ready for real component attachment when:

- the full fake end-to-end scenario passes through the public HTTP API;
- all writes are idempotent and audited;
- job retries, leases, deadlines, OPEN cancellation, and crash recovery work;
- Routing state uses optimistic revision checks;
- CaseView contains no opaque state and Dashboard performs no route logic;
- SVG/video/image references are stable artifacts, not local paths;
- close, export, release, delete, and TTL are separate operations;
- worker transports are replaceable adapters;
- the database can be recreated entirely from migrations;
- PostgreSQL concurrency tests and backup/restore smoke tests pass in the
  deployment environment.

## Implemented runtime shape

The checked-in implementation now contains:

- `javes_main/bootstrap.py`: dependency-injected assembly. Production adapters
  replace ports here.
- `javes_main/application/case_manager.py`: lifecycle orchestration only; it
  never imports a concrete fake.
- `javes_main/application/auth_service.py`: internal IAM with pre-created
  users, Argon2id passwords, opaque hashed sessions, lockout and RBAC.
- `javes_main/persistence/`: in-memory and SQLAlchemy repositories plus six
  ordered Alembic migrations.
- `javes_main/scheduler/`: leased Perception jobs, durable outbox delivery and
  durable Routing triggers. External calls and pure Routing computation happen
  outside long database transactions.
- `javes_contracts/`: canonical boundary imports and deterministic JSON Schema
  generation.
- `javes_fakes/`: Perception, Clip, Retention, Video Archive and Dashboard
  request doubles.
- `javes-main-demo`: complete login-to-delete flow through the public ASGI API.

The account and permission design is normative in `../reference/auth-and-iam.md`. There is
no self-registration endpoint. ROOT is bootstrapped from Main Infra and creates
all later users; the Dashboard only implements login and the actions permitted
by the returned role/permission set.

# JAVES-AI integration fakes

These in-memory fakes let Main Infra and Controls be implemented before real
Perception, Clip, Dashboard, and Site Config components are available. The
Retention core is implemented separately; Fake Video Archive only simulates
the existing external CCTV source for contract tests.

They are contract test doubles, not production workers. They intentionally run
in-process and perform no network, database, video, model, or filesystem work.
Main Infra should access them through the same application ports that real
adapters will implement later.

## Run

```bash
python -m javes_fakes.demo
python -m pytest -q tests/test_integration_fakes.py
```

The component demo prints one deterministic JSON document containing:

- target-gallery setup;
- a Perception job and result;
- a confirmed clip and merged export;
- a close-case retention result;
- Dashboard-shaped HTTP requests.

For the complete Main Infra flow through the real Controls handlers, internal
IAM and scheduler, run:

```bash
javes-main-demo
```

`examples/dashboard_requests.json` contains checked-in requests for API tests.

## Fake Perception

Implementation: `javes_fakes/perception.py`.

`FakePerceptionWorker` accepts explicit `FakeObservation` fixtures and exposes:

```python
gallery = worker.update_gallery(command)
result = worker.process_probe(job, now=now, close_stream=False)
```

It implements:

- target gallery creation/update;
- finite camera windows;
- meaningful `DONE` with no candidates;
- forced `FAILED`, `TIMEOUT`, and `UNAVAILABLE` outcomes;
- partial `coverage_ratio`;
- actual/cumulative `processed_duration_s`;
- stable tracklet and candidate IDs;
- source-frame xywh bboxes;
- OPEN tracklet upsert and explicit stream close;
- calibration and association metadata;
- optional stable `clip_ref`.

It does not perform detection, tracking, Re-ID, decoding, or score calibration.
Observations are fixture truth.

The complete fake system also provides `InMemoryLiveFeedStore`. Tests can put a
JPEG/MJPEG/HLS payload under an opaque `stream_ref`, submit a versioned
`LiveFeedUpdate`, and verify that Controls exposes it only through the
case-authorised `/v1/cases/{case_id}/live` endpoint. The stream reference itself
is never returned to Dashboard.

## Fake Clip

Implementation: `javes_fakes/clip.py`.

`FakeClipWorker` exposes:

```python
record = worker.cut(command)
record = worker.change_state(command)
export = worker.export(command)
```

It implements:

- deterministic, idempotent cut requests;
- `READY` and `UNAVAILABLE` processing outcomes;
- lifecycle states `HOLD`, `CONFIRMED`, and `RELEASED`;
- ordered export of confirmed clips;
- stable artifact references and checksums;
- rejection of reused request IDs with different payloads.

It never invokes ffmpeg and never writes a video. Returned artifact references
are synthetic but have the intended production shape.

## Fake Retention

Implementation: `javes_fakes/retention.py`.

`FakeRetentionWorker` registers resource metadata and processes:

- `CLOSE_CASE`: record the retention anchor without deleting case resources;
- `DELETE_CASE`: delete every non-audit, non-legal-hold resource;
- `TTL_EXPIRED`: delete due or case-TTL-inherited, non-held resources.

It returns sorted deletion/preservation decisions and an audit tombstone ID. It
does not remove real files. The policy mirrors the approved 14-day case TTL and
permanent audit boundary, while real physical deletion is tested in
`javes_retention/`. Re-registering the same resource ID for the same case
updates its retention class, which models HOLD becoming CONFIRMED or
RELEASED. A delete/TTL tombstone also removes any non-held/non-audit resource
registered late; resources registered after `DELETE_CASE` or `TTL_EXPIRED`
are immediately eligible for cleanup.

## Fake Video Archive

Implementation: `javes_fakes/video_archive.py`.

`FakeVideoArchive` accepts explicit per-camera availability windows and answers
versioned segment requests with:

- `AVAILABLE` for full coverage;
- `PARTIAL` plus exact covered bounds and ratio;
- `UNAVAILABLE` plus a stable error code;
- a stable source/archive reference;
- UTC-corrected source-clock semantics;
- OPEN requests clipped to the injected `now`.

It does not decode, store or stream video. Routing/Main/Retention never call
this port. Perception and Clip consume the external archive read-only through
their own adapters.

## Fake Dashboard requests

Implementation: `javes_fakes/dashboard.py`.

`FakeDashboardRequests` creates HTTP-shaped requests for:

- `POST /v1/auth/login`;
- `POST /v1/cases`;
- `GET /v1/cases/{case_id}/view`;
- `POST /v1/cases/{case_id}/decisions`;
- `POST /v1/cases/{case_id}/commands/close`;
- `POST /v1/cases/{case_id}/commands/export`;
- `POST /v1/cases/{case_id}/commands/delete`.

Every protected request uses a bearer session and every mutating request carries
an idempotency key. There is deliberately no signup request. User accounts are
pre-created by ROOT through Main Infra as described in
[`auth-and-iam.md`](auth-and-iam.md). The
standard fixture includes both a rejected alternative and a confirmed
candidate.

## Replacement rule

Main Infra must depend on ports such as `PerceptionPort`, `ClipPort`, and
`RetentionPort`, never directly on fake classes. During tests, dependency
injection supplies these in-memory implementations. Later adapters may use an
HTTP API, a queue, subprocesses, or a managed job system without changing the
application services.

The real worker handoff and acceptance requirements are in
[`perception-clip.md`](perception-clip.md). A fake passing does not prove production
accuracy, video availability, Re-ID calibration, ffmpeg behaviour, or retention
compliance.

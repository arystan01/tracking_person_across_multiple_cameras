# JAVES-AI

JAVES-AI searches for a missing person across CCTV footage. Main Infra owns
case state and audit; Perception produces tracklets; Routing decides which
cameras and time windows are physically plausible and produces route SVGs.
All production component boundaries are versioned HTTP APIs.

## Dashboard: React/Vite + FastAPI BFF

The supported operator UI is the React application in `frontend/`, served by
the FastAPI BFF in `javes_dashboard`. Browsers call only same-origin
`/dashboard-api/*`; the BFF is the sole Dashboard consumer of Main/Controls.
It must never expose the Controls bearer, artifact `object_key`, SQLite,
CCTV Archive, Perception, Routing, or any `/internal/*` endpoint.

1. Authenticate with `POST /v1/auth/login`. There is no public signup.
2. Upload the reference JPEG/PNG through the BFF with authenticated multipart
   `POST /v1/cases/{case_id}/reference-image`, then pass the returned
   `upload_id` as `reference_upload_id` to `POST /v1/cases`. The browser never
   receives `reference_image_ref`. The upload is bound to
   the authenticated operator and case, expires after 24 hours if unused, and
   cannot be reused.
3. Create and inspect cases with `/v1/cases` and `/v1/cases/{case_id}/view`.
4. Submit operator decisions and close/export/delete commands with an
   `idempotency-key` header. Decision history is available from
   `/v1/cases/{case_id}/decisions`; changing a decision is an explicit revoke
   followed by a new decision, never an in-place mutation.
5. Render `rendered_route_maps[].url` as an image. Routing owns the route
   geometry and SVG; Dashboard only selects a floor and displays it.
6. Display `mode`, `estimate`, alternatives, candidates, clips, progress,
   unobserved windows, and errors exactly as returned by Controls.
7. Fetch the authorised current JPEG from `/v1/cases/{case_id}/live`; do not
   expose the internal `object_key` to the browser.

Local Dashboard verification:

```bash
python -m pip install -e '.[test,infra]'
python -m pytest -q tests/test_dashboard_bff.py tests/test_dashboard_architecture.py
cd frontend && npm ci --legacy-peer-deps && npm run lint && npm test && npm run build
```

Run the BFF after building `frontend/dist`:

```bash
export JAVES_DASHBOARD_CONTROLS_URL=http://127.0.0.1:8080
export JAVES_DASHBOARD_SESSION_KEY='FERNET_KEY'
export JAVES_DASHBOARD_ENVIRONMENT=development
javes-dashboard --host 127.0.0.1 --port 8090
```

The browser must treat returned SVG as an image resource, not inject it as
application HTML. Loading, 401/403, conflict, stale live frame, unavailable
Clip, retry, and offline states are part of the Dashboard acceptance suite.

## Clip worker owner: connect and test

Clip is a pull worker. It receives no direct Python calls from Main and must
not import `javes_main` internals.

1. Claim tasks with
   `POST /internal/v1/workers/clip/tasks/claim` using the Clip service bearer
   token and a stable `worker_id`.
2. Process `CLIP_CUT`, `CLIP_STATE`, `CLIP_EXPORT`, and internal `CLIP_PURGE`
   payloads using the public
   models in `javes_contracts`.
3. Read source footage from the read-only NVIDIA Archive API. Never ask Main or
   Retention to delete an `archive_id`.
4. Write output atomically below `cases/{case_id}/...` in the shared private
   artifact filesystem.
5. Return a `ClipRecord` or `ExportClipsResult` plus an
   `artifact-manifest.v1` containing the exact `object_key`, byte size, SHA-256,
   MIME type, creation time, and retention class.
6. Heartbeat long tasks before the lease expires. Treat delivery as
   at-least-once and persist the task fingerprint/result before callback.
7. POST the result to
   `/internal/v1/workers/clip/tasks/{task_id}/result`; report retryable or
   terminal failure to the sibling `/failure` endpoint.

`javes-clip-worker` now implements real FFmpeg extraction, durable clip states,
and ordered export of confirmed clips. FFmpeg and ffprobe must be on PATH.
See [Clip worker setup and behavior](javes_clip/README.md) for archive window
semantics, replay handling, output format, and deployment limitations.

Contract verification:

```bash
python -m pytest -q tests/test_adapter_acceptance.py \
  tests/test_api_first_transport.py tests/test_clip_worker.py
```

## Runtime topology

```text
Browser ──same-origin──> React/FastAPI Dashboard BFF ──HTTP──> Main/Controls API
                                                               ├──HTTP──> Site Config API
                           │              └──HTTP──> Routing API
                           │                              │
                           │                              └──HTTP──> Site Config API
                           │
                           ├──pull/lease HTTP──> Perception worker ──read-only──> CCTV Archive
                           ├──pull/lease HTTP──> Clip worker       ──read-only──> CCTV Archive
                           └──pull/lease HTTP──> Retention worker

Main, Perception, Clip and Retention share one private artifact filesystem.
```

Main owns case state, durable Perception jobs, transactional outbox messages,
Routing triggers, optimistic case revisions, idempotency records, and permanent
append-only security audit. Routing remains pure below its HTTP shell. Site
Config exposes only exact immutable versions; `latest` is not an API concept.
Operator decisions are append-only. Revoke rebuilds Routing deterministically
from the immutable input ledger and cannot reopen a closed search.

There is no cloud object store. Binary component results are exchanged as
`object_key` manifests, not uploaded in API bodies. Case-owned content is
retained for 14 calendar days after close; complete security audit is kept
indefinitely. The agreed final product scope uses the tracked NVIDIA
`mtmc_12cam` videos through the read-only NVIDIA Archive service; a vendor CCTV
integration and cloud storage are not release requirements.

## Root/admin and operations

Only `ROOT` has `case.legal_hold.manage` and `audit.export` by default. A legal
hold requires a unique hold ID, external ticket, reason, actor, timestamp,
idempotency key and expected case revision. Multiple holds may coexist. Delete
and TTL work remains paused until the last hold is released; a hold cannot be
added after deletion has already been requested.

Audit exports are ordered redacted NDJSON plus a SHA-256/size manifest. Export
artifacts use the permanent `AUDIT` retention class. Service and worker tokens
support a bounded current/previous rotation window; readiness rejects expired
overlap or development defaults. Operational commands include:

```bash
javes-main-audit-export --help
javes-token-rotation --help
javes-main-backup --help
javes-main-incident-snapshot --help
```

OpenTelemetry traces, metrics and redacted logs are enabled with
`JAVES_OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT`. Invalid enabled
configuration fails startup/readiness instead of silently disabling export.
The versioned staging topology is under `deploy/staging/`; it contains the
Dashboard BFF/React runtime,
PostgreSQL, the API/scheduler/services/workers, shared artifact storage, NVIDIA
Archive, the FFmpeg Clip worker and an OTel backend.

## Internal APIs

Main worker transport:

```text
POST /internal/v1/workers/{perception|clip|retention}/tasks/claim
POST /internal/v1/workers/{component}/tasks/{task_id}/heartbeat
POST /internal/v1/workers/{component}/tasks/{task_id}/result
POST /internal/v1/workers/{component}/tasks/{task_id}/failure
POST /internal/v1/cases/{case_id}/live
```

Routing:

```text
POST /internal/v1/routing/init
POST /internal/v1/routing/step
```

Site Config:

```text
GET /internal/v1/sites/{site_id}/versions/{version}
GET /internal/v1/sites/{site_id}/versions/{version}/assets/{asset_id}
GET /internal/v1/routing-params/{version}
```

Every worker task has a signed, attempt-scoped lease token. A heartbeat returns
a replacement token and invalidates the old one. Duplicate callbacks with the
same content are accepted; the same task ID with different content is rejected.
Each component has its own bearer token.

## Install and tests

Python 3.11+:

```bash
python -m pip install -e '.[test,infra]'
python -m pytest -q -m 'not real_cv'
```

The normal suite uses deterministic doubles only where isolation is the point.
The opt-in real-CV smoke test uses the tracked videos and actual YOLO/OSNet
weights—no fake detector or embedder:

```bash
python -m pip install -e '.[test,infra,perception]'
JAVES_RUN_REAL_CV=1 python -m pytest -q tests/test_perception_real_smoke.py
```

Run the two inspectable API-first E2E scenarios (real NVIDIA video -> real
Perception -> Main worker API -> Routing API -> Controls SVG) with:

```bash
JAVES_RUN_REAL_CV=1 python -m pytest -q -s tests/test_real_cv_e2e.py
JAVES_RUN_REAL_CV=1 python -m pytest -q -s tests/test_real_cv_blind_gap_e2e.py
```

Open `artifacts/real-cv-e2e/full-minute/index.html` for the complete 60-second
Cam001/Cam003 scenario, or `artifacts/real-cv-e2e/blind-gap/index.html` for the
Cam009 -> Cam006 blind-corridor and `MAX_GAP` scenario. Each synchronized report
contains the source videos, final Routing SVG, event timeline, candidate review
decisions, and links to the generated JSON. Videos are symlinked rather than
copied. `LATEST.txt` points to the immutable run directory. Set
`JAVES_REAL_CV_E2E_OUT` or `JAVES_REAL_CV_BLIND_GAP_OUT` to publish a scenario
elsewhere.

CI keeps fast contract/unit tests and the real-CV job separate. The real-CV job
is available through manual workflow dispatch because model execution is much
heavier than the normal test suite.

Release evidence is accepted only through `javes-nvidia-acceptance` v2. Every
referenced dataset/site/params/model/report file is confined to the bundle
root, opened, size-checked, SHA-256 checked and schema-checked where applicable.
Recorded hashes without readable bytes cannot produce `PASS`.

## Configuration and start order

Site Config uses this filesystem layout:

```text
SITE_CONFIG_ROOT/
  sites/{site_id}/{version}/site.yaml
  sites/{site_id}/{version}/...map assets...
  routing_params/{version}.yaml
```

Start the services in dependency order:

```bash
javes-site-config-api --root "$SITE_CONFIG_ROOT" --token "$SITE_TOKEN"
javes-nvidia-archive-api --root data/nvidia_mtmc/mtmc_12cam \
  --token "$ARCHIVE_TOKEN" --verify
javes-routing-api --site-config-url http://127.0.0.1:8082 \
  --site-config-token "$SITE_TOKEN" --token "$ROUTING_TOKEN"

export JAVES_DATABASE_URL=sqlite:///javes-main.sqlite
export JAVES_ARTIFACT_ROOT=/srv/javes/artifacts
export JAVES_SITE_CONFIG_URL=http://127.0.0.1:8082
export JAVES_SITE_CONFIG_TOKEN="$SITE_TOKEN"
export JAVES_ROUTING_URL=http://127.0.0.1:8081
export JAVES_ROUTING_TOKEN="$ROUTING_TOKEN"
export JAVES_ROUTING_PARAMS_VERSION=1
export JAVES_PERCEPTION_WORKER_TOKEN="$PERCEPTION_TOKEN"
export JAVES_CLIP_WORKER_TOKEN="$CLIP_TOKEN"
export JAVES_RETENTION_WORKER_TOKEN="$RETENTION_TOKEN"
export JAVES_WORKER_LEASE_SECRET="$LEASE_SECRET"

javes-main-migrate --database-url "$JAVES_DATABASE_URL" upgrade
javes-main-api
javes-main-scheduler

javes-perception-worker --main-url http://127.0.0.1:8080 \
  --token "$PERCEPTION_TOKEN" --worker-id perception-1 \
  --config perception.yaml --artifact-root "$JAVES_ARTIFACT_ROOT" \
  --archive-root "$NVIDIA_ROOT" \
  --state-db perception-state.sqlite \
  --archive-url "$NVIDIA_ARCHIVE_URL" --archive-token "$ARCHIVE_TOKEN"

javes-clip-worker --main-url http://127.0.0.1:8080 \
  --token "$CLIP_TOKEN" --worker-id clip-1 \
  --artifact-root "$JAVES_ARTIFACT_ROOT" --archive-root "$NVIDIA_ROOT" \
  --state-db clip-state.sqlite \
  --archive-url "$NVIDIA_ARCHIVE_URL" --archive-token "$ARCHIVE_TOKEN"

javes-retention-worker --main-url http://127.0.0.1:8080 \
  --token "$RETENTION_TOKEN" --worker-id retention-1 \
  --artifact-root "$JAVES_ARTIFACT_ROOT" --state-db retention-state.sqlite

javes-clip-worker --main-url http://127.0.0.1:8080 \
  --token "$CLIP_TOKEN" --worker-id clip-1 \
  --artifact-root "$JAVES_ARTIFACT_ROOT" --state-db clip-state.sqlite \
  --archive-url "$CCTV_ARCHIVE_URL" --archive-token "$CCTV_ARCHIVE_TOKEN"
```

Run API and scheduler as separate processes against the same migrated database.
Production composition is API-first. Deterministic doubles and in-memory
assembly helpers live only under `tests/support`.

The Dashboard, real Clip media worker and authenticated reference-image
case-creation flow are integrated. The accepted Dashboard and Clip consumer
contracts are frozen in `contracts/javes-boundary.v1/`; future breaking changes
require v2. The final product is not release-ready yet: real-CV identity quality
remains below its 12/12 gates, Routing calibration and external operational/
NVIDIA acceptance rehearsals remain incomplete.

## Perception

`javes_perception` is the real YOLO26 + ByteTrack + OSNet-AIN implementation.
The HTTP worker persists completed task fingerprints/results, target-gallery
commands, and OPEN probe checkpoints in SQLite. After restart it re-delivers a
stored result and resumes OPEN processing at the last processed timestamp
instead of scanning from the original start.

Confirmed tracklets are sent back as durable `GALLERY_BUILD` tasks. Gallery,
crop, embedding and live JPEG files use stable case-scoped object keys and are
registered with Retention through manifests. The configured model version is
`osnet_ain_x1_0_msmt17`, not a fake placeholder.

The standalone local diagnostic command remains available:

```bash
javes-perception-probe --config perception.yaml --reference target.jpg \
  --camera cam_1 --from 2026-11-05T05:00:00Z \
  --to 2026-11-05T05:00:10Z --out perception_out
```

## Routing ownership

The algorithm under `javes_routing/core/` remains deterministic and pure:

```python
state = init_state(case, site_graph, params)
result = step(state, probe_results, site_graph, anchors, params, now)
maps = render_route_maps(result, site_graph, floor_assets)
```

The Routing HTTP shell fetches the exact pinned site/params versions, invokes
the pure core, and returns `StepResult` plus SVG. Main stores the result only
through its existing case-revision CAS. A stale response is rejected and the
durable Routing trigger is retried.

## Repository map

- `javes_main/`: Controls API, IAM, cases, durable jobs/outbox/triggers and scheduler.
- `javes_contracts/`: public business and HTTP transport contracts.
- `javes_routing/core/`: pure Routing algorithm, renderer and simulator.
- `javes_routing/service/`: stateless Routing HTTP service shell.
- `javes_site_config/`: immutable filesystem-backed Site Config service.
- `javes_perception/`: real CV worker and durable HTTP runtime.
- `javes_retention/`: durable inventory, deletion journal and HTTP runtime.
- `javes_clip/`: durable FFmpeg Clip worker, Archive adapter and media lifecycle.
- `javes_dashboard/` + `frontend/`: authenticated BFF and React operator UI.
- `contracts/javes-boundary.v1/`: stable schemas, golden payloads and inventory.
- `tests/`: unit, contract, HTTP, persistence, restart and integration tests;
  test doubles are isolated in `tests/support`.

# Perception and Clip integration requirements

Status: **PROVISIONAL v0.1**. Routing implements the consumer side of the
Perception contract described below. The real Perception and Clip workers have
not yet been connected, so their owners must confirm the contract with real
payloads and tests before it is frozen as v1.

This document is the detailed integration checklist for Contracts 3, 5, 6, 7,
12, and 14 from the team architecture. [`../contracts.md`](../contracts.md) remains the short
system-wide overview.

## Global boundary rules

- JSON field names are canonical. Internal Python aliases are not part of the
  public contract.
- All timestamps are ISO-8601 with an explicit UTC offset. Naive datetimes are
  invalid. Camera clock correction is applied by Video Archive/Perception
  before data reaches Routing or Clip and must not be applied twice.
- A bounding box is `[x, y, width, height]` in pixels of the original camera
  frame.
- Every request and result has a stable idempotency identifier.
- Delivery may be at least once. Consumers must tolerate duplicate messages;
  producers must not generate a new logical object when the same id is retried.
- `case_id`, `probe_id`, `tracklet_id`, `candidate_id`, and `clip_id` are opaque
  strings. Their internal format must not be parsed by another component.
- JAVES-AI-owned video, image, embedding, and SVG payloads live in the private
  filesystem-backed `FileArtifactStore`. Database rows and worker messages use
  stable relative `object_key` values, never raw host/container paths. External
  CCTV footage is referenced read-only by `archive_id`.
- Runtime-invalid data is logged and skipped where safe. Invalid site config or
  an incompatible contract version is a hard integration error.

## Perception worker

### Responsibility

Perception answers a precise camera/time request. It detects and tracks people,
compares them with the case target gallery, and returns facts. It does not
choose the next camera, infer a physical route, change a Routing mode, or draw
the route.

Perception is expected to support:

1. target-gallery creation from the reference image;
2. historical finite-window probes;
3. an OPEN live subscription, or an explicitly documented temporary substitute;
4. tracklets and Re-ID candidates;
5. explicit empty success, failure, timeout, and unavailable-video outcomes;
6. actual processing/coverage telemetry;
7. stable tracklet updates during live tracking;
8. optional live-frame output for the Dashboard through Main Infra.

### Input from Routing through Main Infra

One `ProbeRequest` always targets one camera:

```json
{
  "probe_id": "p_41",
  "camera_id": "B",
  "t_from": "2026-10-16T05:16:00Z",
  "t_to": "2026-10-16T05:18:00Z",
  "priority": 0.72,
  "reason": "ROUTE_EXPANSION",
  "hypothesis_ids": ["h_1"],
  "retry_count": 0,
  "retry_of": null
}
```

For live monitoring, `t_to` is the literal string `"OPEN"`:

```json
{
  "probe_id": "p_live_1",
  "camera_id": "B",
  "t_from": "2026-10-16T05:20:00Z",
  "t_to": "OPEN",
  "priority": 0.9,
  "reason": "LIVE_TRACKING",
  "hypothesis_ids": ["h_1"],
  "retry_count": 0,
  "retry_of": null
}
```

Main Infra may wrap the request in a job envelope:

```json
{
  "contract_version": "perception-job.v1",
  "job_id": "j_88",
  "case_id": "c_17",
  "kind": "PROBE",
  "target_gallery_ref": "cases/c_17/perception/gallery/gallery_id.npy",
  "probe": {}
}
```

`job_id`, `case_id`, dispatch state, lease, and target-gallery reference belong
to Main Infra. `priority`, `reason`, and `hypothesis_ids` are scheduling and
audit metadata. Perception must not interpret them as route evidence and should
ignore unknown `reason` values.

Before integration, the Perception owner must confirm how the reference image,
target embeddings, and later operator-confirmed gallery additions are supplied.
Routing does not receive, persist, or manipulate embedding vectors.

### Output to Routing through Main Infra

```json
{
  "probe_id": "p_41",
  "status": "DONE",
  "coverage_ratio": 1.0,
  "processed_duration_s": 120.0,
  "n_tracklets_total": 7,
  "stream_closed": false,
  "tracklets": [
    {
      "tracklet_id": "t_412",
      "case_id": "c_17",
      "probe_id": "p_41",
      "camera_id": "B",
      "t_start": "2026-10-16T05:16:52Z",
      "t_end": "2026-10-16T05:17:11Z",
      "open": false,
      "clipped_start": false,
      "clipped_end": false,
      "bbox_first": [38, 210, 64, 150],
      "bbox_last": [598, 190, 70, 160],
      "crop_paths": [],
      "embedding_path": null,
      "clip_ref": null
    }
  ],
  "candidates": [
    {
      "candidate_id": "cand_03",
      "tracklet_id": "t_412",
      "appearance_score": 0.68,
      "cosine": 0.57,
      "calibration_version": "reid-cal-v1",
      "association_id": null
    }
  ],
  "error_code": null
}
```

Required top-level semantics:

- `probe_id`: the exact request id.
- `status`: `DONE`, `FAILED`, `TIMEOUT`, or `UNAVAILABLE`.
- `tracklets`: tracked people returned for this probe.
- `candidates`: Re-ID matches; every `tracklet_id` must reference a tracklet in
  the same logical result.
- `coverage_ratio`: fraction of the requested finite window that was actually
  decoded and analysed, in `[0, 1]`.
- `processed_duration_s`: actual camera-seconds analysed. For repeated OPEN
  updates it is cumulative for that `probe_id`; Routing charges only the delta.
- `n_tracklets_total`: all people tracked before the Re-ID candidate filter. It
  is not the number of returned candidates.
- `stream_closed`: closes the OPEN subscription itself. It is distinct from a
  person tracklet changing from `open=true` to `open=false`.
- `error_code`: stable machine-readable reason when processing did not finish.

### Empty, failed, and unavailable results

An empty `DONE` is positive evidence that the requested video was checked:

```json
{
  "probe_id": "p_41",
  "status": "DONE",
  "coverage_ratio": 1.0,
  "processed_duration_s": 120.0,
  "n_tracklets_total": 4,
  "stream_closed": false,
  "tracklets": [],
  "candidates": [],
  "error_code": null
}
```

It means "the interval was processed and no target candidate was returned".
Routing may reduce hypotheses that predicted the person at this camera.

Failure is not an empty observation:

```json
{
  "probe_id": "p_41",
  "status": "FAILED",
  "coverage_ratio": 0.0,
  "processed_duration_s": 0.0,
  "n_tracklets_total": 0,
  "stream_closed": false,
  "tracklets": [],
  "candidates": [],
  "error_code": "DECODER_ERROR"
}
```

- `FAILED`: worker error; Main Infra/Routing may retry.
- `TIMEOUT`: deadline expired before a valid result; may retry.
- `UNAVAILABLE`: requested video is absent or outside archive retention; no
  detection miss is inferred and blind retry should normally be avoided.
- Coverage below Routing's configured minimum is recorded as unobserved rather
  than as a reliable miss.

### Tracklet requirements

- `tracklet_id` is stable across retries and OPEN updates for the same logical
  track. Re-sending a tracklet must not create a new id.
- `camera_id` equals the request camera.
- `t_start <= t_end`; for `open=true`, `t_end` is the latest observation time,
  not an invented future end.
- `bbox_first` and `bbox_last` are required because Routing uses site polygons
  to infer allowed entrance and exit edges.
- `clipped_start=true` means the real start may precede `t_start`.
- `clipped_end=true` means the real end may follow `t_end`.
- `crop_paths` and `embedding_path`, if used across services, must resolve to
  shared storage or be replaced by opaque object references. Container-local
  paths are not a production contract.
- `clip_ref` is an optional stable source reference for Clip. It must not be a
  short-lived playback URL.

### Candidate and score requirements

- `candidate_id` is unique at least within a case.
- `tracklet_id` refers to exactly one returned tracklet.
- `appearance_score` is in `[0, 1]`.
- `cosine` is optional raw model telemetry and is not treated as a probability.
- `calibration_version` identifies the model/calibration contract. It may be
  `null` while uncalibrated, but then the score is explicitly only a ranking
  signal and cannot be claimed to have equal meaning across cameras.
- `association_id` is optional. When Perception knows that fragmented
  same-camera tracklets belong to the same person, the same stable association
  id allows Routing to merge them safely. Without it, concurrent people are
  kept separate.
- Perception returns candidates above an agreed low recall-oriented floor;
  Routing performs route-aware ranking and rejection.

The Perception owner must document the score range, neutral point, calibration
dataset, model version, cross-camera comparability, and measured per-camera
detection probability. The architecture statement "0.8 means the same on every
camera" is a target requirement, not a fact until calibration proves it.

### OPEN lifecycle

1. Routing issues a probe with `t_to="OPEN"`.
2. Main Infra keeps the job/subscription active.
3. Perception may emit repeated `DONE` updates using the same `probe_id`.
4. An ongoing person keeps the same `tracklet_id`, `open=true`, and advances
   `t_end`, bbox, and score metadata.
5. When that person leaves, the same tracklet is emitted with `open=false`.
6. The camera subscription remains active until `stream_closed=true` or Main
   Infra explicitly cancels it.
7. `processed_duration_s` is cumulative for the subscription.

If the current Perception worker cannot stream, its owner must describe the
temporary polling/chunk contract. Main Infra may adapt chunks to this lifecycle;
Routing should not acquire Perception-specific polling logic.

### LiveFeed output

Live frames are not Routing input. Perception writes target-annotated frames to
a bounded TTL buffer and publishes the latest metadata to the worker-authenticated
Main Infra endpoint. The implemented contract is:

```json
{
  "contract_version": "live-feed-update.v1",
  "update_id": "live-update-881",
  "case_id": "c_17",
  "camera_id": "B",
  "tracklet_id": "t_412",
  "bbox": [38, 210, 64, 150],
  "observed_at": "2026-10-16T05:20:03Z",
  "stream_ref": "live:c_17:B",
  "stream_kind": "JPEG",
  "expires_at": "2026-10-16T05:20:08Z"
}
```

`stream_ref` is opaque and is resolved by a replaceable `LiveFeedPort`. Main
Infra converts it into the authorised
`GET /v1/cases/{case_id}/live` endpoint, computes `age_s`, and puts that URL in
CaseView. Dashboard never receives the raw stream reference or storage
credentials. The fake stack uses `InMemoryLiveFeedStore`; production may proxy
or redirect to MJPEG/HLS according to the real adapter.

### Perception handoff checklist

The Perception owner must provide all of the following:

- current Pydantic/JSON Schema or equivalent models;
- one real finite-window request and successful result;
- one real `DONE` result with no candidates;
- one real error/unavailable result;
- one OPEN update and explicit subscription-close example, or a documented gap;
- target-gallery creation and update interface;
- score/calibration description and model version;
- `coverage_ratio`, `processed_duration_s`, and `n_tracklets_total` definitions;
- bbox format and source-frame dimensions;
- idempotency and stable-ID guarantees;
- `clip_ref`, crop, and embedding storage-reference formats;
- local startup command and dependency/runtime versions.

Main Infra integration is accepted only when these examples validate against
the frozen shared models and an end-to-end stub test passes.

## Clip worker

### Responsibility

Clip cuts a video segment from Video Archive, preserves or releases it according
to commands from Main Infra, and can merge confirmed clips for export. It does
not decide whether a candidate is correct, calculate route order, close a case,
or delete unrelated case data.

Routing never calls Clip directly. Main Infra watches surviving/confirmed route
evidence and sends idempotent Clip commands. Routing only preserves stable
`tracklet_id`, `candidate_id`, visit order, and optional `clip_ref` metadata.

The source `archive_id` belongs to the existing CCTV system and is read-only.
Clip outputs (`ClipRecord.artifact_ref` and export `artifact_ref`) must point to
product-owned storage through `object_key` or an owned URI. Retention never
deletes or registers the source `archive_id`.

### Source reference

Preferred tracklet source metadata:

```json
{
  "tracklet_id": "t_412",
  "camera_id": "B",
  "t_start": "2026-10-16T05:16:52Z",
  "t_end": "2026-10-16T05:17:11Z",
  "clip_ref": {
    "archive_id": "archive-B-20261016",
    "camera_id": "B"
  }
}
```

Clip must confirm whether `camera_id + t_from + t_to` is sufficient or an
additional archive/object identifier is required. `clip_ref` is opaque to
Routing and interpreted only by Video Archive/Clip.

### Proposed cut request

```json
{
  "contract_version": "clip-command.v1",
  "clip_request_id": "cr_501",
  "case_id": "c_17",
  "candidate_id": "cand_03",
  "tracklet_id": "t_412",
  "camera_id": "B",
  "t_from": "2026-10-16T05:16:47Z",
  "t_to": "2026-10-16T05:17:16Z",
  "padding_before_s": 5,
  "padding_after_s": 5,
  "source_ref": {
    "archive_id": "archive-B-20261016"
  },
  "initial_state": "HOLD"
}
```

Main Infra chooses padding and state. Clip reports the actual cut interval; it
does not silently claim unavailable padding was included.

### Proposed result and ClipRecord

```json
{
  "clip_request_id": "cr_501",
  "clip_id": "clip_901",
  "case_id": "c_17",
  "candidate_id": "cand_03",
  "tracklet_id": "t_412",
  "status": "READY",
  "state": "HOLD",
  "camera_id": "B",
  "t_start": "2026-10-16T05:16:47Z",
  "t_end": "2026-10-16T05:17:16Z",
  "mime_type": "video/mp4",
  "duration_s": 29.0,
  "artifact_ref": {
    "object_key": "cases/c_17/clips/clip_901.mp4"
  },
  "checksum": "sha256:...",
  "error_code": null
}
```

Processing status and retention state are deliberately separate:

- processing `status`: `PENDING`, `READY`, `FAILED`, `UNAVAILABLE`;
- lifecycle `state`: `HOLD`, `CONFIRMED`, `RELEASED`.

`RELEASED` means the case no longer requires protection from normal cleanup. It
is not proof that bytes were already deleted. Actual deletion is performed by
Retention according to policy and recorded separately.

Main Infra registers a newly cut HOLD clip with Retention class
`REVIEW_HOLD`, not generic `EPHEMERAL`, so closing search does not destroy
material that still awaits operator review. CONFIRM changes the class to
`CONFIRMED_EVIDENCE`; RELEASE changes it to `EPHEMERAL` and permits cleanup.

### Lifecycle commands

Main Infra should issue explicit idempotent commands:

```json
{
  "command_id": "cc_701",
  "case_id": "c_17",
  "clip_id": "clip_901",
  "action": "CONFIRM",
  "operator_id": "op_12",
  "occurred_at": "2026-10-16T05:31:00Z"
}
```

Required actions and ownership:

- `HOLD`: protect a clip belonging to a surviving hypothesis.
- `CONFIRM`: retain evidence after an operator confirms the candidate.
- `RELEASE`: remove the active hold after reject/prune; Retention may later
  delete the artifact under policy.
- `EXPORT`: merge confirmed clips in an explicit route order supplied by Main
  Infra.
- Physical `DELETE` should normally be a Retention command, not an implicit
  consequence of reject or case close.

Case `CLOSE`, result `SAVE/EXPORT`, and case `DELETE` are different commands.
Closing stops active search and subscriptions. Export produces retained output.
Delete requests policy-controlled removal and must not be inferred from close.
The implemented MVP lifecycle requires CLOSE before EXPORT; a repeated CLOSE
does not downgrade a `SAVED` case.

### Merge/export request

```json
{
  "export_request_id": "ex_81",
  "case_id": "c_17",
  "ordered_clip_ids": ["clip_901", "clip_905", "clip_918"],
  "format": "video/mp4"
}
```

The order comes from the confirmed Routing visits through Main Infra. Clip must
not derive route order from creation timestamps alone. The result should return
a stable export artifact reference, checksum, actual duration, status, and any
missing input clip ids.

### Clip reliability requirements

- Repeating one `clip_request_id`, `command_id`, or `export_request_id` is safe.
- One logical request must not create duplicate protected artifacts.
- Actual source and output intervals are reported.
- Missing or expired source video returns `UNAVAILABLE`, not false success.
- Permanent metadata stores an `artifact_ref` with a relative `object_key`, not
  a raw filesystem path or temporary playback URL.
- Files are written atomically or exposed only after `READY`.
- Every state transition and export is auditable by actor, timestamp, and command
  id.
- Concurrent HOLD/CONFIRM/RELEASE commands use a version or deterministic state
  transition policy.
- Clip does not delete embeddings, crops, route state, or audit rows; Retention
  owns cross-resource deletion.

### Clip handoff checklist

The Clip owner must provide:

- current request, response, command, and ClipRecord schemas;
- a real successful cut example;
- an unavailable-source example;
- supported codecs/containers and ffmpeg requirements;
- source reference format and Video Archive interface;
- actual padding and timestamp behaviour;
- processing and lifecycle states;
- idempotency/concurrency behaviour;
- `FileArtifactStore` object-key format, TTL, checksum, authorised-read and
  physical-delete behavior;
- merge/export request and response examples;
- local startup command and runtime dependencies.

## Integration acceptance

Perception is ready to connect when:

- real payloads validate without manual field renaming;
- finite empty success is distinguishable from failure/unavailable;
- stable tracklet IDs and bbox semantics are demonstrated;
- score/calibration status is explicit;
- actual coverage and processing duration are available or a documented adapter
  supplies conservative values;
- OPEN works end to end, or an explicit temporary polling adapter is tested.

Clip is ready to connect when:

- a Perception tracklet/source reference can produce one idempotent clip;
- HOLD, CONFIRM, and RELEASE transitions are explicit;
- export consumes an ordered list from Main Infra;
- stable artifact references are separated from playback URLs;
- source unavailability and cleanup ownership are unambiguous.

Until then, Main Infra should use fake adapters that emit exactly these payloads.
Worker-specific fields belong behind adapters rather than leaking into Routing,
CaseView, or database consumers.

The checked-in test implementations and their limitations are documented in
[`fakes.md`](fakes.md).

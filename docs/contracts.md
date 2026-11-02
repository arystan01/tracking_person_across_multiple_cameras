# Routing integration contracts

All timestamps are ISO-8601 with an offset and are normalized to UTC. Bounding boxes are `[x, y, width, height]`. Every `camera_id` must exist in the pinned `SiteGraph`. On the wire, an open window is `"t_to": "OPEN"`.

The detailed normative expectations, lifecycle semantics, concrete payloads,
and handoff checklists for the still-unconnected Perception and Clip workers are
in [`reference/perception-clip.md`](reference/perception-clip.md). That document is provisional until both
worker owners confirm it with real payloads; Main Infra should integrate worker
implementations through versioned adapters rather than silently changing these
Routing-facing contracts.

Contract-faithful in-memory doubles and Dashboard request fixtures are described
in [`reference/fakes.md`](reference/fakes.md); they are integration scaffolding, not evidence that real
video/model/ffmpeg/retention components work.

## Case Manager / API → Routing

The Case Manager owns persistence, revision checks, jobs, and heartbeat scheduling. It passes the previous opaque `SearchState`, terminal/streaming results, operator anchors, the pinned site and parameters, and an explicit `now`.

```json
{
  "state": {"case_id": "c_17", "state_version": 11},
  "probe_results": [],
  "anchors": {
    "confirm_tracklet_ids": [],
    "reject_tracklet_ids": [],
    "operator_closed": false
  },
  "now": "2026-10-16T05:20:00Z"
}
```

It must atomically persist the returned state only if its expected revision still matches. It calls Routing after a result, every `heartbeat_s`, and after operator input. Camera-minute budget comes from `processed_duration_s` reported by the worker, not requested window length.

`operator_closed=true` closes the active Routing search. SAVE/export and DELETE/retention are separate Case Manager commands and must not be translated into `operator_closed` implicitly.

After `step()`, it calls:

```python
rendered = render_route_maps(step_result, pinned_site_graph, floor_assets)
```

Routing returns `StepResult` plus a `RenderedRouteBundle`. API may save SVGs and replace inline payloads with URLs, but must not recompute geometry.

## Routing → Perception

One request means one camera:

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

Perception may wrap this with its own `job_id`, `case_id`, and `kind: "PROBE"` fields. Those job fields are owned by the Case Manager, not by Routing.

## Perception → Routing

```json
{
  "probe_id": "p_41",
  "status": "DONE",
  "coverage_ratio": 1.0,
  "processed_duration_s": 120.0,
  "n_tracklets_total": 7,
  "stream_closed": false,
  "tracklets": [{
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
    "clip_ref": null
  }],
  "candidates": [{
    "candidate_id": "cand_03",
    "tracklet_id": "t_412",
    "appearance_score": 0.68,
    "cosine": 0.57,
    "calibration_version": "reid-cal-v1"
  }]
}
```

`DONE` with empty arrays is meaningful. `FAILED`, `TIMEOUT`, and `UNAVAILABLE` are not misses. OPEN updates reuse the same `tracklet_id`; `stream_closed=true` explicitly closes the subscription. Clock offsets are already applied before this boundary.

`processed_duration_s` is actual camera-seconds analysed and is cumulative for
repeated OPEN updates with the same `probe_id`. `n_tracklets_total` counts all
tracked people before the Re-ID candidate filter. Bboxes are source-frame
`[x, y, width, height]`. Scores without a `calibration_version` are ranking
signals, not calibrated probabilities.

## Routing → Dashboard through CaseView

```json
{
  "case_id": "c_17",
  "revision": 12,
  "mode": "WAITING",
  "best_hypothesis": {},
  "estimate": {},
  "rendered_route_maps": [{
    "floor_id": "1F",
    "mime_type": "image/svg+xml",
    "url": "/cases/c_17/routes/12/1F.svg",
    "alt_text": "Best route on floor 1F: A to B"
  }]
}
```

Dashboard displays the SVG, switches floors, refreshes when `revision` changes, and shows mode/estimate/review data. It does not calculate graph paths, draw lines/gaps/estimates, or change `mode`.

Live frames are a separate Perception → Main Infra boundary. Perception submits
`LiveFeedUpdate` metadata with an opaque TTL-bound `stream_ref`; CaseView exposes
only the authorised `/v1/cases/{case_id}/live` URL, bbox and age. Routing never
reads or produces live frames.

## Site Config / Infrastructure → Routing

Provides versioned YAML with floor raster path and size, camera/blind-node floor positions, frame regions, directed edge timings, optional edge `map_path`, vertical transitions, OUTSIDE entrances, and dwell models. The service loading the site reads floor files and passes their bytes to the pure renderer keyed by floor ID or configured image path.

## Clip, Archive, and Retention

Routing never calls these workers. Case Manager creates HOLDs for tracklets in surviving hypotheses, confirms/releases them after operator decisions, and orders export clips by confirmed visits. The existing external CCTV Archive is a read-only source used only by Perception and Clip. Retention receives distinct close/delete/TTL events and stores final route JSON/SVG according to policy; it never deletes `archive_id`.

Clip processing status (`PENDING | READY | FAILED | UNAVAILABLE`) is separate
from lifecycle state (`HOLD | CONFIRMED | RELEASED`). `RELEASED` authorises
policy-based cleanup but does not claim that the bytes have already been
deleted. Clip consumes a stable `clip_ref`/archive reference, returns a stable
artifact reference, and merges an explicit ordered list of confirmed clip IDs.
Case close, export/save, release, and delete are separate commands.

`CLOSE_CASE` removes `EPHEMERAL` and `REVIEW_HOLD`; confirmed evidence, case
results and the complete append-only audit remain. `DELETE_CASE` removes every
non-held, non-audit resource. `TTL_EXPIRED` is emitted at the case retention deadline;
`expires_at=null` inherits that deadline, while an explicit future expiry is
handled by the Retention maintenance sweep. A `RetentionResult` is final proof,
not merely queue acknowledgement.

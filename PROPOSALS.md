# PROPOSALS.md — Proposed changes to shared contracts

These proposals are for discussion with the team. Routing currently works with backward-compatible defaults if the other side has not implemented them yet.

Implementation status as of the internal fake-stack milestone:

- implemented locally with backward-compatible defaults: 2, 3, 4, 7, 8, 9,
  11, 12 and 13;
- still requires confirmation from the real Perception/Clip/Dashboard owners:
  1, 6 and 10;
- later production-hardening scope: operator revoke/manual sightings from 5.

Local implementation does not by itself freeze a shared contract. Real worker
payloads still have to pass `docs/reference/perception-clip.md` before v1 is
declared stable.

## 1. Bbox format (Arystan)
- **Current**: Routing assumes `bbox = (x, y, w, h)` per contract 6.
- **Proposal**: Confirm this is correct. If Perception uses `(x1, y1, x2, y2)`, we need to agree.
- **Default**: Routing converts internally using `bbox_to_xyxy()`.

## 2. ProbeResult extensions (Arystan, Sihyun)
- **New optional fields**:
  - `coverage_ratio: float = 1.0` — fraction of the window actually decoded (0–1)
  - `n_tracklets_total: int | None = None` — everyone tracked in the window
  - New `status: UNAVAILABLE` — outside archive retention
- **Why**: An empty probe on a broken camera should not be counted as a miss.
- **Default**: `coverage_ratio=1.0`, `n_tracklets_total=None`, no UNAVAILABLE → v1 behaviour.

## 3. t_to wire format (Sihyun)
- **Proposal**: Use the string `"OPEN"` instead of `null` for open-ended probes at the JSON boundary.
- **Default**: Routing internally uses `None`; serialization layer converts.

## 4. Heartbeat calling cadence (Sihyun, Case manager)
- **Proposal**: Case manager calls `step()` on every ProbeResult AND every `heartbeat_s` (5s) with an empty result list.
- **Why**: `step()` is pure and takes `now`, so time-based transitions only happen when called.

## 5. Case profile and operator extensions (Sihyun, Jungwoo)
- **New fields**:
  - `CaseRequest.profile: str = 'ADULT'` — ADULT | CHILD | ELDERLY
  - `Anchors.revoke: list[str] = []` — cancel a confirm or reject
  - `Anchors.manual_sightings: list[dict] = []` — witness reports
  - `StepResult.needs_review: list[str] = []` — tracklet IDs needing operator review
  - `StepResult.suggest_gallery_add: list[str] = []` — tracklets to add to gallery
- **Default**: All empty/default → v1 behaviour.

## 6. Dashboard rejected candidates (Jungwoo)
- **Proposal**: Dashboard should show the top `rejected[]` candidates with `reject_reason`, `appearance` score, and `gate_margin_s`, and let the operator override (confirm creates a GATE_OVERRIDE hypothesis).

## 7. Routing-owned route SVG (Sihyun, Jungwoo)
- **Proposal**: Case Manager calls Routing's pure `render_route_maps()` after accepting a `StepResult` and stores or serves each returned floor SVG.
- `CaseView` carries `rendered_route_maps[{floor_id, mime_type, url|svg, alt_text}]` and `route_revision`.
- Dashboard displays the finished SVG and switches floors. It does not compute path coordinates or draw route, gap, alternative, or estimate overlays.

## 8. OPEN subscription closure (Arystan, Sihyun)
- **Proposal**: Add optional `ProbeResult.stream_closed: bool = false`.
- **Why**: a closed tracklet ends one person track, while `stream_closed=true` ends the camera subscription itself. They are separate lifecycle events.

## 9. Cross-tracklet association (Arystan)
- **Proposal**: Add optional `Candidate.association_id` when Perception knows two fragmented same-camera tracklets belong to the same person.
- **Default**: Without an explicit association Routing keeps them as separate visits/branches; temporal and spatial closeness alone is not enough in a crowd.

## 10. Versioned Perception adapter (Arystan, Main Infra)
- **Proposal**: Freeze the Routing-facing `ProbeRequest`/`ProbeResult` contract after validating real Perception payloads from `docs/reference/perception-clip.md`. Put worker-specific job, gallery, queue, and model fields in a versioned Main Infra adapter envelope.
- **Why**: Routing should not change whenever the Perception runtime or queue implementation changes.
- **Default**: The current Routing contracts and fake Perception are provisional v0; no production compatibility is claimed yet.

## 11. Actual work accounting (Arystan, Main Infra)
- **Proposal**: `processed_duration_s` means actual camera-seconds analysed and is cumulative for repeated OPEN results sharing a `probe_id`. Main Infra/Routing charges only the positive delta.
- **Why**: Requested window duration overcharges partial coverage, retries, outages, and streaming updates.
- **Default**: For a finite result that omits the field, Routing conservatively derives requested duration multiplied by `coverage_ratio`.

## 12. Clip processing versus lifecycle (Clip, Retention, Main Infra)
- **Proposal**: Keep processing status (`PENDING | READY | FAILED | UNAVAILABLE`) separate from lifecycle state (`HOLD | CONFIRMED | RELEASED`). `RELEASED` permits Retention cleanup but is not the same as physical deletion.
- **Why**: Reject, release, case close, export, and delete are separate audited actions.
- **Default**: Routing has no direct Clip call. Main Infra uses fake adapters until the Clip contract and real examples in `docs/reference/perception-clip.md` are confirmed.

## 13. Stable cross-service artifact references (Perception, Clip, Main Infra)
- **Proposal**: Replace process/container-local absolute paths and expiring
  playback URLs at service boundaries with stable references. JAVES-AI-owned
  files use an `object_key`, which is a relative path inside the private
  `FileArtifactStore`; CCTV inputs use an external read-only `archive_id`.
  Generate authorised playback URLs at the Main API edge.
- **Why**: Raw host paths and temporary URLs cannot safely serve as durable
  database identifiers. No separate cloud/object storage is planned.

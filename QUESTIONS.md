# QUESTIONS.md — Assumptions and open questions

## Assumptions made

### Contract & Format Assumptions
1. **Shared contracts**: Contracts 3, 6, and 7 are maintained locally in `routing/contracts/` with backward-compatible defaults until centralized shared contracts are deployed.
2. **Bounding box format**: Contract 6 specifies `bbox = [x, y, w, h]` (top-left x, top-left y, width, height) in pixel coordinates. Routing internally converts to `(x1, y1, x2, y2)` at the boundary via `bbox_to_xyxy()`.
3. **Wire format for open probes**: Open-ended probes represent `t_to` as the string `"OPEN"` on the JSON wire boundary, deserialized to Python `None` internally.
4. **Camera clock offsets & drift**: Clock offsets are pre-applied upstream; an additional `clock_tolerance_s = 3.0s` tolerance is assumed to absorb residual NVR clock jitter (`dt < t_min - clock_tolerance_s`).
5. **Deterministic IDs and serialization**: State counters and content hashes are used instead of `uuid4()` or timestamps for IDs. Sets are serialized as sorted lists, and explicit tie-breakers (`(-score, id)`, `(-priority, camera, t_from)`) ensure cross-process JSON determinism across different `PYTHONHASHSEED` values.

### Algorithm & Spatio-Temporal Assumptions
6. **Log-odds appearance scoring**: Appearance scores `a` are mapped to log-odds `log(a / (1 - a))` clipped to `[score_clip_min, 1 - score_clip_min]`. Scores are recomputed from all accumulated evidence on each round rather than incrementally summed, ensuring consistent beam re-ranking and supporting operator undo.
7. **Gate speed & Subject profiles**: Hard gating uses `gate_speed = 4.0 m/s` (accommodating running children), walk speed defaults to `1.3 m/s` for `t_typ`, and `t_max = 2.5 * t_typ`. `CaseRequest.profile` (`ADULT`, `CHILD`, `ELDERLY`) scales edge timings according to `profiles` multipliers in `params.yaml`.
8. **Arrival-time probability mass**: Miss penalties apply continuous arrival mass `F` over the lognormal transit distribution covered by DONE probes (`log(1 - p_det * F)`), replacing all-or-nothing window coverage. Tail probes continue until remaining mass < `tail_eps` (0.05).
9. **Calling cadence**: The Case Manager calls `step()` upon receipt of any `ProbeResult` AND every `heartbeat_s` (5s) with an empty result list. `step()` is pure and idempotent for identical `now`.
10. **Crowd handling & Visit merging**: Different candidate detections in a probe spawn separate hypothesis branches (keeping top `cands_per_probe = 3`). Tracklet merging is restricted to tracklets on the same camera with temporal gap ≤ `merge_gap_s` (10s) and spatial distance ≤ `merge_dist_frac` (0.25 frame diagonal). Visit appearance score is the mean of the top 2 candidate scores.
11. **Incomplete graph & Operator overrides**: A physically gated candidate is not attached automatically. If the operator confirms it, Routing creates a low-weight `UNKNOWN` transition tagged `GATE_OVERRIDE` using `leak_prior`.
12. **Probe coverage & Outages**: Probes with `coverage_ratio < min_coverage` (0.5) or status `UNAVAILABLE` are marked `unobserved` and do not trigger detection miss penalties.
13. **Map and geometry preservation**: Map data (`map`, `floor`, `map_xy`, `map_path`) in `site.yaml` must survive `SiteGraph` loading, validation, and JSON serialization without being dropped or renamed.

---

## Open questions for team members

### 1. Perception (Arystan)
- **Bbox format**: Can you confirm whether Perception outputs `[x, y, w, h]` or `[x1, y1, x2, y2]`?
- **Score calibration**: Does `score = 0.5` represent uninformative / random chance? The log-odds formulation assumes `score > 0.5` is positive evidence and `score < 0.5` is negative evidence.
- **ProbeResult telemetry**: Are `coverage_ratio` (fraction decoded) and `n_tracklets_total` supported or planned for video decoding pipelines?
- **Actual processing**: Can Perception report cumulative `processed_duration_s` per `probe_id`, especially for OPEN jobs?
- **Stable live identity**: Does an OPEN track keep the same `tracklet_id` across updates, and how is the subscription itself explicitly closed?
- **Target gallery**: How are the initial reference and later confirmed-tracklet embeddings supplied and versioned?
- **Storage references**: What are the real cross-service formats for crops, embeddings, live buffers, and `clip_ref`? Container-local file paths are not sufficient.
- **Association**: Is there a stable same-person `association_id` for fragmented tracklets on one camera?
- **Required handoff**: Please provide the schemas and real success, empty, failure, unavailable, and OPEN payloads listed in `docs/reference/perception-clip.md`.

### 2. Clip worker
- **Source lookup**: Is `camera_id + t_from + t_to` sufficient, or which `archive_id`/object reference is required?
- **State model**: Are processing status and lifecycle state separate? Routing/Main Infra expects `PENDING | READY | FAILED | UNAVAILABLE` separately from `HOLD | CONFIRMED | RELEASED`.
- **Idempotency**: What keys prevent duplicate cuts, state transitions, and merged exports?
- **Retention boundary**: Does Clip only release artifacts for cleanup, while Retention performs physical deletion?
- **Export**: Can Clip merge an explicit ordered list of confirmed `clip_id` values and report missing inputs?
- **Required handoff**: Please provide the schemas, real cut/unavailable examples, source-reference format, lifecycle commands, and export examples listed in `docs/reference/perception-clip.md`.

### 2a. Retention integration details

- **Resolved retention horizon**: Case content is retained for 14 calendar days
  after `CLOSE_CASE`; the complete append-only audit and resources under legal
  hold are excluded from automatic content deletion.
- **Legal hold authority**: Which role/service may set and release a hold, and
  what external case/ticket ID must be recorded with that decision?
- **Product-owned replicas**: Which ArtifactStore or Perception/Clip stores
  contain replicas, and which final deletion receipt do they expose?
- **Owned locators**: Perception/Clip must map every generated crop, embedding,
  live buffer and output artifact to `object_key` or an owned URI scheme with a
  Retention backend. CCTV `archive_id` is read-only and outside our retention.

### 3. Case Store / API

Resolved in the internal Main Infra contour:

- Case Manager persists durable heartbeat triggers using Routing's configured
  cadence.
- `t_to="OPEN"` is supported at the wire boundary and OPEN jobs are cancellable
  subscriptions.
- Routing-generated SVG is persisted through `ArtifactStore` and served with a
  private immutable URL per `route_revision`.
- accepted results, Routing runs/state/revisions and operator decisions are
  retained as immutable case-data records until policy deletion.

Still open for a later contract revision:

- **Operator undo / revoke**: exact UX, authorization and evidence replay rules
  for undoing a confirm/reject. The current MVP stores enough history but does
  not expose a revoke command.

### 4. Dashboard (Jungwoo)
- **Rejected candidates**: How should the UI present top `rejected[]` hypotheses with their `reject_reason` and `gate_margin_s` so operators can trigger `GATE_OVERRIDE`?
- **SVG display boundary**: Can the Dashboard confirm it will display Routing-produced SVG without rebuilding route geometry, while supporting floor switching and accessible `alt_text`?
- **Closed-account UX**: Dashboard must implement login/password, forced
  password change and permission-aware controls, but no self-registration.
- **Live endpoint**: confirm JPEG/MJPEG/HLS rendering against the authorised
  `CaseView.live.stream_url`; Dashboard must never use `stream_ref` directly.

### 5. Security / operations

- MFA is explicitly deferred beyond the MVP. Decide later whether to use TOTP,
  WebAuthn or an external identity provider.
- Production session storage, TLS termination, secret rotation and audit export
  belong to deployment/security review; the internal contract is documented in
  `docs/reference/auth-and-iam.md`.

### 6. Perception (YOLO26 + ByteTrack + OSNet)

- **Score calibration**: `appearance_score = sigmoid(26.0 * cosine - 15.49)`,
  fitted on Market-1501 same/different pairs (`osnet-ain-market1501-logit-v1`,
  see `docs/perception.md`). On the NVIDIA 12-camera warehouse strangers have
  median cosine 0.64 vs 0.46 on Market, so the calibration must be fitted per
  site; ranking is still correct. `p_det` is not measured yet.
- **Gallery model_version**: Case Manager sends `TargetGalleryCommand` with the
  default `model_version="fake-reid-v1"`; the worker echoes it because the
  acceptance check requires equality. Case Manager should send
  `osnet_ain_x1_0_msmt17`.
- **OPEN probes**: every update re-processes `[t_from, now]` so tracklet ids stay
  stable. Fine for short live windows, expensive for long ones; needs a per
  camera/chunk cache. LiveFeed (contract 14) is not produced yet.
- **Video source**: `LocalVideoArchive` reads mp4 files from `perception.yaml`
  (camera -> file + UTC start). The real CCTV archive adapter is still open.

# Real-component attachment checklist

This archived short checklist is superseded by the normative detailed
specification in [`../components.md`](../components.md), the deployment runbook
in [`../deployment.md`](../deployment.md), and the executable matrix in
[`../testing.md`](../testing.md).

The internal Main Infra/Controls contour is complete against fakes. The items
below are the remaining real integration and deployment work. Application
services, Routing logic and Dashboard projections should not be rewritten to
fit a worker; translate at the adapter boundary.

## Perception

- Implement `PerceptionPort.update_gallery`, `execute` and `cancel`.
- Validate real payloads against generated `TargetGallery*`,
  `PerceptionProbeJob` and `ProbeResult` schemas.
- Demonstrate finite success, DONE-empty, FAILED, TIMEOUT, UNAVAILABLE and OPEN
  update/close.
- Preserve stable tracklet IDs, UTC-corrected time, source-frame xywh bbox,
  actual cumulative `processed_duration_s` and coverage.
- Publish `LiveFeedUpdate` to the worker-authenticated endpoint and put frames
  behind a production `LiveFeedPort` implementation.
- Complete every handoff item in `../reference/perception-clip.md`.

## Clip

- Implement `ClipPort.cut`, `change_state` and `export`.
- Accept idempotent HOLD/CONFIRM/RELEASE commands and ordered export requests.
- Resolve the stable Perception `clip_ref` through Video Archive.
- Return stable artifact references, actual intervals, checksums and explicit
  FAILED/UNAVAILABLE outcomes.
- Pass the early operator-decision, crash replay, post-close review and ordered
  export conformance tests.

## Retention

- Durable Retention core and physical `FileArtifactStore` deletion are already
  implemented; connect only the real Perception/gallery and Clip-output cleanup
  handlers.
- Preserve `REVIEW_HOLD`, confirmed evidence and legal holds under the approved
  policy; process CLOSE, TTL and DELETE as distinct events.
- Keep case-wide delete tombstones so late registrations are also removed.
- Make every command idempotent and return an auditable `RetentionResult`.
- Keep final case content for 14 calendar days after close and keep the complete
  actor/time/action/details audit forever.

## Video Archive

- This is an existing external CCTV system, not a JAVES-AI component.
- Perception and Clip own their read-only integration with it and translate
  full/partial/unavailable coverage into JAVES-AI contracts.
- Main Infra, Routing and Retention never call it or delete `archive_id`.
- Source-video retention belongs to the external CCTV owner.

## Site Config

- Implement `SiteConfigPort.get_graph`, `get_floor_assets` and versioned
  `get_params`.
- Treat graph, floor bytes and Routing parameters as immutable per version.
- Expose a site/catalog selection flow for Dashboard case creation.
- Validate canonical YAML and all referenced cameras/edges/assets before a
  version becomes selectable.

## Dashboard

- Implement login, forced password change and logout; do not implement signup.
- Use `GET /v1/cases` and `CaseView`; poll or subscribe according to the chosen
  deployment transport.
- Display Routing-produced SVG URLs without drawing route geometry.
- Render candidates, review state, clip/export status, errors, estimates,
  watch cameras and the authorised live URL.
- Hide unavailable controls using returned permissions, while relying on
  server-side RBAC as the security boundary.
- Send idempotency keys and the authenticated `user_id` as `operator_id`.

## Deployment

- Run API and scheduler processes against PostgreSQL and a persistent private
  `FileArtifactStore` root.
- Apply Alembic migrations before startup and bootstrap exactly one ROOT.
- Configure TLS, worker identity/secrets, session policy, backups and restore.
- Run the PostgreSQL multi-worker lease/CAS lane, filesystem atomicity test,
  backup/restore smoke and real-adapter contract suite.
- Add monitoring for health, queue age, retries, latency, camera-seconds and
  failed/unobserved work.

MFA, operator decision revoke/replay, manual witness sightings and empirical
score calibration remain explicitly later hardening features, not hidden gaps
in the current PDF-MVP attachment contour.

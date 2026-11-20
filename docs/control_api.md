# Control API handoff

This is a normalized Python interface for the prototype, not a finalized network schema. The lifecycle/mode/reason names and time ownership follow the team agreement. Endpoints, authentication, field nesting, job receipts, and post-completion review policy still need exact examples from the control module.

## Requests

- `create_case(title, operator, camera_id, last_known_time, reference, notes)`: no recording window, timeout, or search-mode input. The server owns creation time and starts preparation.
- `get_case_view(case_id)`: returns a complete snapshot. Partial/delta responses must be assembled by the client adapter before reaching the UI.
- `submit_decision(case_id, candidate_id, decision, operator)`: only CONFIRM or REJECT. Accepting a request does not mean routing has recalculated the route.
- `close_case(case_id, operator)`: requests staff closure, eventually yielding CLOSED + PAST_SUMMARY + done=true + OPERATOR_CLOSED.
- `delete_case(case_id, operator)`: retained local-demo feature; map to the team's authorized deletion operation only when that API is agreed.

The demo implements these methods in-process. A real modular-monolith adapter can do the same; HTTP is not required.

## Snapshot fields

```text
id, title, operator, last_known {camera_id, time}
window {start, end}, created_at
lifecycle, mode, done, stop_reason
revision, route_revision
candidates[], route[], errors[], unobserved_windows[]
reference_b64, notes, merged_clip_url
```

Only the demo emits `source` and internal `simulation` metadata. JSON exports remove the reference photo and internal simulator metadata.

Candidate fields currently normalized as:

```text
id, camera_id, observed_at, appearance_score
observation_type: NORMAL | LONG_GAP
decision: UNREVIEWED | CONFIRMED | REJECTED
clip_state: HOLD | CONFIRMED | RELEASED
review_state: READY | PENDING | APPLIED
requested_decision: null | CONFIRM | REJECT
review_revision, explanation, clip_url
```

`review_state`, `requested_decision`, and `review_revision` are prototype adapter fields, not claims that the team's API already exposes these names. Confirm the real acknowledgment/job-completion fields. Translate them into these values, or keep receipts in the client adapter until an authoritative snapshot confirms the corresponding decision and routing result. Never infer completion solely from a case revision increase, because new sightings can also increment it.

The UI accepts fresh snapshots on changes to either revision and rejects regressions. A revision must change whenever displayable data changes. The control layer must provide monotonic revisions scoped to a case. The demo persists pending requests so reconnecting does not lose their status.

Route is an ordered backend-supplied list of observations (`camera_id`, `observed_at`, `source`, optional `candidate_id`). The UI connects those points for display, with no ranking or hypothesis selection. Coordinate/graph configuration is still a four-camera demo in domain.py. Agree on a site-map payload before supporting other buildings.

## Review and lifecycle assumptions needing confirmation

- Search continues during review/recalculation. The demo applies decisions after 3 seconds of simulated worker latency.
- LONG_GAP requires confirmation before inclusion in the main route. All other demo observations also require review; real routing may supply provisional hypotheses directly.
- Conflicting decisions are rejected. The demo treats retries of the same decision as idempotent; agree on the real request ID/idempotency mechanism.
- Post-completion review was not specified. The UI conservatively makes CLOSED and FAILED cases read-only. Confirm whether final review is supported later.
- Operator closure during a pending job was not specified. The demo blocks close while a decision is pending. Agree whether the real control API drains or cancels those jobs.
- "Save case" is removed because state already persists and routing owns termination. Download report is a snapshot export, not closure.
- The demo has no real worker retry, authorization, or media preservation. Clip URLs must come from the media/control module.

## Error distinction

Critical case failure is lifecycle=FAILED. Camera/worker errors stay in errors[] and unobserved_windows without changing lifecycle. WAITING is active search, not failure or completion. Only the backend sets done and stop_reason.

The operator-facing Download report button exports PDF. JsonExport remains an internal serialization adapter; the PDF excludes raw backend fields, reference image data, and simulator metadata.

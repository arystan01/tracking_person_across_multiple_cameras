# Routing MVP verification

Run `python -m pytest -q` from the repository root.

| Area | Status | Evidence |
|---|---|---|
| Canonical PDF names + legacy aliases | implemented | `routing/contracts/__init__.py`, `tests/test_mvp_contracts.py` |
| Aware UTC times; naive rejection | implemented | contract validators and canonical-wire test |
| Start from camera/zone `last_known` | implemented | `routing/step.py::_anchor_hypotheses` |
| Deterministic non-empty `step()` and JSON round trip | implemented | non-empty purity test |
| Graph/time hard gate; clipped only removes lateness | implemented | `routing/update.py::gate_visit_details` |
| Multi-hypothesis branches and exact edge IDs | implemented | `Hypothesis.transitions`, `update_hypotheses` |
| DONE-empty arrival-mass penalty | implemented | `_arrival_mass`, `recompute_hypothesis_score` |
| Retry limit and unobserved windows | implemented | `_process_probe_results`, retry test |
| OPEN upsert and explicit close | implemented | `stream_closed`, OPEN lifecycle test |
| LONG_GAP held until confirm | implemented | `_branch_with_visit`, long-gap test |
| Modes and stop priority | implemented | `routing/predict.py`, algorithm tests |
| One-round all-camera baseline | implemented | `routing/step.py` |
| Site map/floor/path round trip | implemented | `routing/site.py`, canonical-site test |
| Routing-owned deterministic SVG | implemented | `routing/render.py`, multifloor/fallback tests |
| Seeded simulator, demo, CSV benchmark | implemented | `routing/demo.py`, `routing/sim/bench.py` |
| Real Perception contract validation | pending external integration | `perception-clip.md` handoff checklist |
| Real Clip lifecycle/export validation | pending external integration | `perception-clip.md` handoff checklist |

The SVG test parses every output as XML, checks byte determinism, multiple floors, exact map paths, vertical markers, raster data URIs, abstract fallback, and absence of scripts/external image links.

## Main Infra and Controls verification

| Area | Status | Evidence |
|---|---|---|
| Closed internal IAM; no signup | implemented | `auth-and-iam.md`, `../../tests/test_auth.py` |
| Role permissions and delete restriction | implemented | `ROLE_PERMISSIONS`, Controls RBAC tests |
| SQL persistence and migrations | implemented | Alembic 0001-0006, restart/migration tests |
| Durable leases, deadlines and OPEN cancellation | implemented | `tests/test_scheduler.py`, `tests/test_persistence.py` |
| Transactional outbox and crash replay | implemented | outbox crash/idempotency tests |
| Pure Routing outside long DB transaction | implemented | durable Routing trigger/CAS replay test |
| Immutable accepted results and case history | implemented | `probe_results`, `case_data`, audit tests |
| SVG and artifact serving | implemented | HTTP and restart tests |
| HOLD/CONFIRM/RELEASE and ordered export | implemented | normal and early-decision race tests |
| Close/export/delete/TTL separation | implemented with real Retention core | physical deletion, restart/resume, tombstone and auto-TTL tests |
| Case list, CaseView and live proxy | implemented | Controls API/live-feed tests |
| Operational metrics | implemented | API metrics and cumulative OPEN accounting tests |
| Clean wheel, schemas and packaged migrations | verified locally | `pip wheel`, clean-install demo/migration/schema commands |
| Real Perception/Clip, Dashboard and Site Config | pending integration | adapter handoff documents |
| Existing external CCTV Archive | out of product scope | read-only access owned by Perception/Clip |
| PostgreSQL backup/concurrency smoke | pending deployment environment | deployment acceptance step |

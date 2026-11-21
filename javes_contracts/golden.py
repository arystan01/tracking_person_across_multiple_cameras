"""Canonical, secret-free examples for producer/consumer conformance tests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from . import (
    AuditExportManifest,
    AuditExportRequest,
    CaseCommandBody,
    CaseView,
    CreateCaseBody,
    CreateLegalHoldBody,
    DecisionRecordView,
    EvidenceBundleVerification,
    EvidenceRef,
    LegalHoldView,
    LoginRequest,
    LoginResponse,
    ProblemDetails,
    ProbeResult,
    RetentionCommand,
    RetentionHoldCommand,
    RetentionHoldResult,
    RetentionResult,
    ReviewDecisionBody,
    ReleaseLegalHoldBody,
    RevokeDecisionBody,
    RotationStatusResponse,
    ServiceHealthResponse,
    RoutingInitRequest,
    RoutingInitResponse,
    RoutingParamsResponse,
    RoutingStepRequest,
    RoutingStepResponse,
    SiteVersionResponse,
    TargetGalleryResult,
    VideoSegmentRequest,
    VideoSegmentResult,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerTaskAck,
    WorkerTaskFailureRequest,
    WorkerTaskResultRequest,
)


T0 = "2026-01-02T03:04:05Z"
T1 = "2026-01-02T03:05:05Z"
SHA = "0" * 64


def _state() -> dict[str, Any]:
    return {
        "case_id": "case-001",
        "site_version": "site-v1",
        "params_version": "params-v1",
        "window_start": T0,
        "window_end": T1,
        "mode": "CATCHING_UP",
    }


def _step_result() -> dict[str, Any]:
    return {
        "state": _state(),
        "probes": [],
        "hypotheses": [],
        "best": None,
        "mode": "CATCHING_UP",
        "done": False,
    }


def _site_graph() -> dict[str, Any]:
    return {
        "site": "nvidia-12cam",
        "version": "site-v1",
        "cameras": {"Cam001": {"id": "Cam001"}},
        "edges": [],
    }


Golden = tuple[type[BaseModel], dict[str, Any]]


GOLDEN_FIXTURES: dict[str, Golden] = {
    "operations/health-ready.json": (
        ServiceHealthResponse,
        {"status": "ready", "telemetry": "ready"},
    ),
    "operations/rotation-status.json": (
        RotationStatusResponse,
        {
            "perception_worker": {
                "current_key_id": "sha256:0123456789abcdef",
                "previous_key_id": None,
                "previous_valid_until": None,
                "previous_accepted": False,
            }
        },
    ),
    "operations/audit-export-request.json": (
        AuditExportRequest,
        {"export_id": "audit-2026-001", "from_time": T0, "to_time": T1},
    ),
    "operations/audit-export-manifest.json": (
        AuditExportManifest,
        {
            "export_id": "audit-2026-001",
            "created_at": T1,
            "created_by": "root-001",
            "filters": {"from_time": T0, "to_time": T1},
            "row_count": 0,
            "size_bytes": 0,
            "sha256": SHA,
            "events_url": "/v1/audit/exports/audit-2026-001/events",
            "manifest_url": "/v1/audit/exports/audit-2026-001/manifest",
        },
    ),
    "release/evidence-ref.json": (
        EvidenceRef,
        {
            "name": "fast-python-3.13",
            "path": "suites/fast-python-3.13.json",
            "size_bytes": 123,
            "sha256": SHA,
            "media_type": "application/json",
            "schema_id": "javes-release-evidence-document.v1",
        },
    ),
    "release/evidence-verification.json": (
        EvidenceBundleVerification,
        {
            "manifest_sha256": SHA,
            "files": [
                {
                    "name": "fast-python-3.13",
                    "path": "suites/fast-python-3.13.json",
                    "media_type": "application/json",
                    "declared_sha256": SHA,
                    "computed_sha256": SHA,
                    "declared_size_bytes": 123,
                    "computed_size_bytes": 123,
                    "schema_valid": True,
                    "passed": True,
                }
            ],
        },
    ),
    "controls/login-request.json": (
        LoginRequest,
        {"username": "operator", "password": "example-only"},
    ),
    "controls/login-response.json": (
        LoginResponse,
        {
            "access_token": "example-token-not-a-secret",
            "expires_at": T1,
            "user": {
                "user_id": "user-001",
                "username": "operator",
                "role": "OPERATOR",
                "permissions": ["case.read", "case.review"],
                "must_change_password": False,
            },
        },
    ),
    "controls/create-case.json": (
        CreateCaseBody,
        {
            "case_id": "case-001",
            "operator_id": "user-001",
            "target_id": "target-001",
            "reference_image_ref": {"object_key": "cases/case-001/reference.jpg"},
            "last_known": {"camera_id": "Cam001", "time": T0},
            "window": {"start": T0, "end": T1},
            "site_id": "nvidia-12cam",
            "site_version": "site-v1",
        },
    ),
    "controls/case-view.json": (
        CaseView,
        {
            "case_id": "case-001",
            "revision": 3,
            "route_revision": 1,
            "lifecycle": "SEARCHING",
            "mode": "CATCHING_UP",
            "search_progress": {"camera_minutes_used": 1.0},
        },
    ),
    "controls/decision.json": (
        ReviewDecisionBody,
        {
            "decision_id": "decision-001",
            "candidate_id": "candidate-001",
            "tracklet_id": "tracklet-001",
            "decision": "CONFIRM",
            "operator_id": "user-001",
            "occurred_at": T1,
        },
    ),
    "controls/decision-revoke.json": (
        RevokeDecisionBody,
        {
            "revocation_id": "revoke-001",
            "decision_id": "decision-001",
            "operator_id": "user-001",
            "reason": "Incorrect candidate",
            "occurred_at": T1,
            "expected_case_revision": 3,
        },
    ),
    "controls/decision-record.json": (
        DecisionRecordView,
        {
            "decision_id": "decision-001",
            "candidate_id": "candidate-001",
            "tracklet_id": "tracklet-001",
            "decision": "CONFIRM",
            "operator_id": "user-001",
            "occurred_at": T1,
            "active": False,
            "revocation_id": "revoke-001",
            "revoked_by": "user-001",
            "revoked_at": T1,
            "revocation_reason": "Incorrect candidate",
        },
    ),
    "controls/legal-hold-create.json": (
        CreateLegalHoldBody,
        {
            "hold_id": "hold-001",
            "external_ticket_id": "LEGAL-2026-001",
            "reason": "Preservation request",
            "operator_id": "root-001",
            "occurred_at": T1,
            "expected_case_revision": 3,
        },
    ),
    "controls/legal-hold-release.json": (
        ReleaseLegalHoldBody,
        {
            "release_id": "hold-release-001",
            "hold_id": "hold-001",
            "external_ticket_id": "LEGAL-2026-002",
            "reason": "Preservation request closed",
            "operator_id": "root-001",
            "occurred_at": T1,
            "expected_case_revision": 4,
        },
    ),
    "controls/legal-hold-view.json": (
        LegalHoldView,
        {
            "hold_id": "hold-001",
            "case_id": "case-001",
            "external_ticket_id": "LEGAL-2026-001",
            "reason": "Preservation request",
            "set_by": "root-001",
            "set_at": T1,
            "active": True,
        },
    ),
    "controls/command.json": (
        CaseCommandBody,
        {
            "command_id": "command-001",
            "action": "CLOSE",
            "operator_id": "user-001",
            "occurred_at": T1,
        },
    ),
    "controls/problem-retryable.json": (
        ProblemDetails,
        {
            "type": "urn:javes:problem:revision-conflict",
            "title": "Revision Conflict",
            "status": 409,
            "detail": "reload the case and retry",
            "code": "REVISION_CONFLICT",
            "retryable": True,
        },
    ),
    "controls/problem-replay-incomplete.json": (
        ProblemDetails,
        {
            "type": "urn:javes:problem:replay-evidence-incomplete",
            "title": "Replay Evidence Incomplete",
            "status": 409,
            "detail": "stored evidence is incomplete",
            "code": "REPLAY_EVIDENCE_INCOMPLETE",
            "retryable": False,
        },
    ),
    "routing/init-request.json": (
        RoutingInitRequest,
        {
            "case": {
                "case_id": "case-001",
                "target_id": "target-001",
                "start_time": T0,
                "window_end": T1,
            },
            "anchors": {"last_known_camera": "Cam001", "last_known_time": T0},
            "site_id": "nvidia-12cam",
            "site_version": "site-v1",
            "params_version": "params-v1",
            "now": T0,
        },
    ),
    "routing/init-response.json": (
        RoutingInitResponse,
        {"state": _state(), "site_checksum": SHA, "params_checksum": SHA},
    ),
    "routing/step-request.json": (
        RoutingStepRequest,
        {
            "state": _state(),
            "probe_results": [],
            "anchors": {},
            "site_id": "nvidia-12cam",
            "site_version": "site-v1",
            "params_version": "params-v1",
            "now": T0,
        },
    ),
    "routing/step-response.json": (
        RoutingStepResponse,
        {
            "result": {
                "step_result": _step_result(),
                "rendered_route": {"route_revision": 1, "maps": []},
            },
            "site_checksum": SHA,
            "params_checksum": SHA,
        },
    ),
    "workers/claim-request.json": (
        WorkerClaimRequest,
        {"worker_id": "perception-01", "limit": 1, "lease_seconds": 30},
    ),
    "workers/claim-empty.json": (WorkerClaimResponse, {"tasks": []}),
    "workers/heartbeat-request.json": (
        WorkerHeartbeatRequest,
        {"lease_token": "opaque-example", "extend_seconds": 30},
    ),
    "workers/heartbeat-cancel.json": (
        WorkerHeartbeatResponse,
        {"task_id": "task-001", "cancel_requested": True},
    ),
    "workers/result.json": (
        WorkerTaskResultRequest,
        {"lease_token": "opaque-example", "result": {"status": "DONE"}},
    ),
    "workers/failure.json": (
        WorkerTaskFailureRequest,
        {
            "lease_token": "opaque-example",
            "error_code": "UPSTREAM_TIMEOUT",
            "retryable": True,
        },
    ),
    "workers/duplicate-ack.json": (
        WorkerTaskAck,
        {"task_id": "task-001", "accepted": True, "duplicate": True, "terminal": True},
    ),
    "perception/probe-success.json": (
        ProbeResult,
        {
            "probe_id": "probe-001",
            "status": "DONE",
            "tracklets": [
                {
                    "tracklet_id": "tracklet-001",
                    "camera_id": "Cam001",
                    "t_start": T0,
                    "t_end": T1,
                    "bbox_first": [10, 20, 30, 60],
                    "bbox_last": [20, 20, 30, 60],
                }
            ],
            "candidates": [
                {
                    "candidate_id": "candidate-001",
                    "tracklet_id": "tracklet-001",
                    "appearance_score": 0.82,
                }
            ],
            "processed_duration_s": 60.0,
        },
    ),
    "perception/probe-empty.json": (
        ProbeResult,
        {
            "probe_id": "probe-002",
            "status": "DONE",
            "processed_duration_s": 60.0,
            "coverage_ratio": 1.0,
        },
    ),
    "perception/probe-failure.json": (
        ProbeResult,
        {
            "probe_id": "probe-003",
            "status": "FAILED",
            "coverage_ratio": 0.0,
            "error_code": "DECODE_FAILED",
        },
    ),
    "perception/probe-open.json": (
        ProbeResult,
        {
            "probe_id": "probe-open",
            "status": "DONE",
            "processed_duration_s": 5.0,
            "stream_closed": False,
        },
    ),
    "perception/gallery-result.json": (
        TargetGalleryResult,
        {
            "command_id": "gallery-001",
            "case_id": "case-001",
            "gallery_ref": "cases/case-001/gallery.bin",
            "source_count": 1,
            "model_version": "osnet_ain_x1_0_msmt17",
        },
    ),
    "retention/command.json": (
        RetentionCommand,
        {
            "command_id": "retention-001",
            "case_id": "case-001",
            "action": "DELETE_CASE",
            "actor_id": "user-001",
            "occurred_at": T1,
        },
    ),
    "retention/hold-command.json": (
        RetentionHoldCommand,
        {
            "command_id": "retention-hold-001",
            "case_id": "case-001",
            "hold_id": "hold-001",
            "action": "SET",
            "external_ticket_id": "LEGAL-2026-001",
            "reason": "Preservation request",
            "actor_id": "root-001",
            "occurred_at": T1,
        },
    ),
    "retention/hold-result.json": (
        RetentionHoldResult,
        {
            "command_id": "retention-hold-001",
            "case_id": "case-001",
            "hold_id": "hold-001",
            "action": "SET",
            "active_hold_ids": ["hold-001"],
        },
    ),
    "retention/result.json": (
        RetentionResult,
        {
            "command_id": "retention-001",
            "case_id": "case-001",
            "action": "DELETE_CASE",
            "deleted_resource_ids": ["resource-001"],
            "audit_tombstone_id": "audit-001",
            "receipts": [
                {
                    "resource_id": "resource-001",
                    "backend_kind": "shared-filesystem",
                    "locator_kind": "object_key",
                    "locator_hash": f"sha256:{SHA}",
                    "attempt": 1,
                    "status": "DELETED",
                    "occurred_at": T1,
                }
            ],
        },
    ),
    "site/site-version.json": (
        SiteVersionResponse,
        {
            "site_id": "nvidia-12cam",
            "version": "site-v1",
            "checksum": SHA,
            "graph": _site_graph(),
            "assets": [],
        },
    ),
    "site/routing-params.json": (
        RoutingParamsResponse,
        {"version": "params-v1", "checksum": SHA, "params": {"version": "params-v1"}},
    ),
    "archive/segment-request.json": (
        VideoSegmentRequest,
        {"request_id": "archive-001", "camera_id": "Cam001", "t_from": T0, "t_to": T1},
    ),
    "archive/segment-result.json": (
        VideoSegmentResult,
        {
            "request_id": "archive-001",
            "camera_id": "Cam001",
            "availability": "AVAILABLE",
            "requested_from": T0,
            "requested_to": T1,
            "covered_from": T0,
            "covered_to": T1,
            "coverage_ratio": 1.0,
            "source_ref": {"archive_id": "nvidia:Cam001"},
        },
    ),
}


def canonical_golden_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path, (model, payload) in sorted(GOLDEN_FIXTURES.items()):
        documents[path] = model.model_validate(payload).model_dump(
            mode="json", by_alias=True, exclude_none=False
        )
    return documents

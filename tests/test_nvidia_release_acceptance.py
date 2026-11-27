from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from javes_contracts import NvidiaReleaseEvidence as SharedNvidiaReleaseEvidence

from javes_release.acceptance import (
    AcceptancePolicy,
    ComponentDisposition,
    EvidenceStatus,
    EvidenceBundleVerification,
    NvidiaReleaseEvidence,
    VerifiedEvidenceFile,
    evidence_references,
    evaluate_nvidia_release,
)
from javes_release.cli import run


SHA = "a" * 64
COMMIT = "b" * 40


def test_release_gate_uses_the_canonical_shared_evidence_contract() -> None:
    assert NvidiaReleaseEvidence is SharedNvidiaReleaseEvidence


def _ref(name: str, media_type: str = "application/json") -> dict:
    suffix = {
        "application/json": "json",
        "text/html": "html",
        "image/svg+xml": "svg",
        "application/octet-stream": "bin",
    }[media_type]
    return {
        "name": name,
        "path": f"evidence/{name}.{suffix}",
        "size_bytes": 1,
        "sha256": SHA,
        "media_type": media_type,
        "schema_id": (
            "javes-release-evidence-document.v1"
            if media_type == "application/json"
            else None
        ),
    }


def _policy() -> AcceptancePolicy:
    return AcceptancePolicy()


def _evidence() -> NvidiaReleaseEvidence:
    workflow = [
        "login",
        "create-case",
        "immutable-site-pin",
        "gallery",
        "finite-probes",
        "open-live",
        "review-long-gap",
        "closed-review",
        "route-estimate-svg",
        "loss-reachable",
        "outside",
        "re-entry",
        "close",
        "clip-export",
        "retention-delete",
        "main-restart",
        "scheduler-restart",
        "routing-restart",
        "perception-restart",
        "retention-restart",
        "backup-restore",
        "incident-response",
        "rollback",
    ]
    return NvidiaReleaseEvidence.model_validate(
        {
            "schema_version": "javes-nvidia-release-evidence.v2",
            "release_id": "nvidia-release-001",
            "git_commit": COMMIT,
            "release_version": "nvidia-deliverable-v1",
            "dataset_manifest": _ref("dataset-manifest"),
            "site_bundle": _ref("site-bundle"),
            "routing_params": _ref("routing-params"),
            "models": {
                "detector": _ref("detector", "application/octet-stream"),
                "reid": _ref("reid", "application/octet-stream"),
            },
            "profile_fingerprint": SHA,
            "scope": {
                "dashboard": "ACCEPTED",
                "clip_worker": "ACCEPTED",
            },
            "blocks": {
                letter: {"status": "PASS", "evidence": [_ref(f"block-{letter}")]}
                for letter in "ABCDEFGH"
            },
            "suites": {
                name: {"passed": True, "evidence": _ref(name)}
                for name in (
                    "fast-python-3.11",
                    "fast-python-3.13",
                    "cached-real-cv",
                    "cold-api-first-real-cv",
                    "clean-wheel-entrypoints",
                )
            },
            "quality": {
                "active_scenarios": 12,
                "passed_scenarios": 12,
                "quarantined_scenarios": 0,
                "identity_precision": 0.97,
                "identity_recall": 0.95,
                "false_identity_rate": 0.03,
                "tracklet_precision": 0.98,
                "tracklet_recall": 0.94,
                "median_position_error_m": 0.5,
                "p95_position_error_m": 1.1,
                "hidden_point_containment": 0.95,
                "top2_branch_recall": 0.95,
                "search_success_rate": 1.0,
                "target_specific_tuning": False,
            },
            "operations": {
                "controls_api_latency_p95_s": 1.2,
                "search_time_p95_s": 700,
                "mean_camera_minutes": 100,
                "queue_latency_p95_s": 2,
                "worker_latency_p95_s": 500,
                "routing_latency_p95_s": 1,
                "retry_rate": 0.01,
                "deletion_sla_p95_s": 30,
                "audit_complete": True,
                "cold_warm_business_equal": True,
                "restart_repeatable": True,
            },
            "workflow": [
                {"name": name, "status": "PASS", "evidence": _ref(name)}
                for name in workflow
            ],
            "inspectable_artifacts": [
                _ref("report", "text/html"),
                _ref("route", "image/svg+xml"),
                _ref("machine-report", "application/json"),
            ],
        }
    )


def _verification(evidence: NvidiaReleaseEvidence) -> EvidenceBundleVerification:
    return EvidenceBundleVerification(
        manifest_sha256="f" * 64,
        files=[
            VerifiedEvidenceFile(
                name=reference.name,
                path=reference.path,
                media_type=reference.media_type,
                declared_sha256=reference.sha256,
                computed_sha256=reference.sha256,
                declared_size_bytes=reference.size_bytes,
                computed_size_bytes=reference.size_bytes,
                schema_valid=(
                    True if reference.media_type == "application/json" else None
                ),
                passed=True,
            )
            for _, reference in evidence_references(evidence)
        ],
    )


def _evaluate(evidence: NvidiaReleaseEvidence, policy: AcceptancePolicy | None = None):
    return evaluate_nvidia_release(
        evidence, policy or _policy(), _verification(evidence)
    )


def test_complete_release_evidence_passes_deterministically() -> None:
    evidence = _evidence()
    first = _evaluate(evidence)
    second = _evaluate(evidence)
    assert first.status == "PASS"
    assert first == second
    assert first.failures == []


def test_quality_quarantine_and_target_tuning_block_release() -> None:
    evidence = _evidence()
    evidence.quality.quarantined_scenarios = 1
    evidence.quality.false_identity_rate = 0.33
    evidence.quality.target_specific_tuning = True
    report = _evaluate(evidence)
    failed = {item.check_id for item in report.failures}
    assert report.status == "BLOCKED"
    assert "quality.quarantine" in failed
    assert "quality.false-identity-rate" in failed
    assert "quality.no-target-tuning" in failed


def test_unavailable_clip_requires_approval_and_only_waives_export() -> None:
    evidence = _evidence()
    evidence.scope.clip_worker = ComponentDisposition.UNAVAILABLE
    export = next(item for item in evidence.workflow if item.name == "clip-export")
    export.status = EvidenceStatus.WAIVED
    export.reason = "Clip worker is outside the approved intermediate delivery"

    blocked = _evaluate(evidence)
    assert "scope.external-components" in {item.check_id for item in blocked.failures}
    assert "workflow.clip-export" in {item.check_id for item in blocked.failures}

    evidence.scope.intermediate_delivery_approved = True
    evidence.scope.approval_reference = "delivery-decision-001"
    approved = _evaluate(evidence)
    assert approved.status == "PASS"


def test_missing_restart_and_failed_cold_suite_are_reported() -> None:
    evidence = _evidence()
    evidence.workflow = [
        item for item in evidence.workflow if item.name != "routing-restart"
    ]
    evidence.suites["cold-api-first-real-cv"].passed = False
    report = _evaluate(evidence)
    failed = {item.check_id for item in report.failures}
    assert failed >= {
        "workflow.routing-restart",
        "suite.cold-api-first-real-cv",
    }


def test_fixed_operational_thresholds_and_no_monetary_cost_gate() -> None:
    policy = AcceptancePolicy()
    assert policy.max_controls_api_latency_p95_s == 2.0
    assert policy.max_routing_latency_p95_s == 2.0
    assert policy.max_deletion_sla_p95_s == 60.0
    assert "cost" not in policy.model_dump_json()

    evidence = _evidence()
    evidence.operations.controls_api_latency_p95_s = 2.01
    evidence.operations.routing_latency_p95_s = 2.01
    evidence.operations.deletion_sla_p95_s = 60.01
    report = _evaluate(evidence, policy)
    assert {item.check_id for item in report.failures} >= {
        "operations.controls-api-latency",
        "operations.routing-latency",
        "operations.deletion-sla",
    }


def _materialize_bundle(tmp_path: Path) -> tuple[NvidiaReleaseEvidence, Path, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    evidence = _evidence()
    for _, reference in evidence_references(evidence):
        path = bundle / reference.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if reference.media_type == "application/json":
            payload = json.dumps(
                {"schema_version": reference.schema_id, "status": "PASS"},
                sort_keys=True,
            ).encode("utf-8")
        elif reference.media_type == "text/html":
            payload = b"<!doctype html><title>evidence</title>"
        elif reference.media_type == "image/svg+xml":
            payload = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        else:
            payload = (reference.name.encode("utf-8") + b"\0") * 300_000
        path.write_bytes(payload)
        reference.size_bytes = len(payload)
        reference.sha256 = hashlib.sha256(payload).hexdigest()
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return evidence, bundle, evidence_path


def test_cli_writes_inspectable_json_and_html(tmp_path: Path) -> None:
    _, bundle, evidence_path = _materialize_bundle(tmp_path)
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "report"
    policy_path.write_text(
        json.dumps(_policy().model_dump(mode="json")), encoding="utf-8"
    )
    report = run(evidence_path, bundle, output, policy_path)
    assert report.status == "PASS"
    report_json = (output / "report.json").read_text()
    assert json.loads(report_json)["status"] == "PASS"
    assert str(tmp_path) not in report_json
    assert "NVIDIA release sign-off" in (output / "index.html").read_text()


def test_cli_uses_built_in_policy_without_override(tmp_path: Path) -> None:
    _, bundle, evidence_path = _materialize_bundle(tmp_path)
    output = tmp_path / "report"
    assert run(evidence_path, bundle, output).status == "PASS"


@pytest.mark.parametrize("mutation", ["changed", "truncated", "missing"])
def test_changed_truncated_and_missing_bytes_block_release(
    tmp_path: Path, mutation: str
) -> None:
    evidence, bundle, evidence_path = _materialize_bundle(tmp_path)
    target = bundle / evidence.dataset_manifest.path
    if mutation == "changed":
        target.write_bytes(b"changed but not declared")
    elif mutation == "truncated":
        target.write_bytes(target.read_bytes()[:3])
    else:
        target.unlink()
    report = run(evidence_path, bundle, tmp_path / "report")
    assert report.status == "BLOCKED"
    failures = {item.detail for item in report.failures}
    assert failures & {"SIZE_MISMATCH", "SHA256_MISMATCH", "MISSING_OR_ESCAPING_PATH"}


def test_schema_invalid_json_and_git_mismatch_block_release(tmp_path: Path) -> None:
    evidence, bundle, evidence_path = _materialize_bundle(tmp_path)
    target = bundle / evidence.dataset_manifest.path
    payload = b'{"schema_version":"wrong","status":"PASS"}'
    target.write_bytes(payload)
    evidence.dataset_manifest.size_bytes = len(payload)
    evidence.dataset_manifest.sha256 = hashlib.sha256(payload).hexdigest()
    evidence_path.write_text(
        json.dumps(evidence.model_dump(mode="json")), encoding="utf-8"
    )
    report = run(
        evidence_path,
        bundle,
        tmp_path / "report",
        verify_git_commit=True,
        checkout_root=Path(__file__).resolve().parents[1],
    )
    details = {item.detail for item in report.failures}
    assert "SCHEMA_VALIDATION_FAILED" in details
    assert report.verification is not None
    assert report.verification.git_commit_failure_code == "GIT_COMMIT_MISMATCH"


def test_path_traversal_duplicates_and_symlink_escape_are_rejected(
    tmp_path: Path,
) -> None:
    raw = _evidence().model_dump(mode="json")
    raw["dataset_manifest"]["path"] = "../outside.json"
    with pytest.raises(ValidationError, match="bundle-relative"):
        NvidiaReleaseEvidence.model_validate(raw)

    raw = _evidence().model_dump(mode="json")
    raw["dataset_manifest"]["path"] = raw["site_bundle"]["path"]
    with pytest.raises(ValidationError, match="globally unique"):
        NvidiaReleaseEvidence.model_validate(raw)

    evidence, bundle, evidence_path = _materialize_bundle(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target = bundle / evidence.dataset_manifest.path
    target.unlink()
    target.symlink_to(outside)
    report = run(evidence_path, bundle, tmp_path / "report")
    assert "SYMLINK_NOT_ALLOWED" in {item.detail for item in report.failures}


def test_self_reference_and_legacy_v1_are_explicit_failures(tmp_path: Path) -> None:
    evidence, bundle, _ = _materialize_bundle(tmp_path)
    evidence.dataset_manifest.path = "manifest.json"
    self_manifest = bundle / "manifest.json"
    self_manifest.write_text(
        json.dumps(evidence.model_dump(mode="json")), encoding="utf-8"
    )
    report = run(self_manifest, bundle, tmp_path / "self-report")
    assert "SELF_REFERENTIAL_EVIDENCE" in {item.detail for item in report.failures}

    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": "javes-nvidia-release-evidence.v1",
                "release_id": "legacy",
                "git_commit": COMMIT,
                "release_version": "legacy-v1",
            }
        ),
        encoding="utf-8",
    )
    legacy_report = run(legacy, bundle, tmp_path / "legacy-report")
    assert legacy_report.status == "BLOCKED"
    assert legacy_report.failures[0].detail == "UNVERIFIED_LEGACY"

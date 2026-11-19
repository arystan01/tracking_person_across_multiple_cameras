from __future__ import annotations

import json
from pathlib import Path

from javes_release.acceptance import (
    AcceptancePolicy,
    ComponentDisposition,
    EvidenceStatus,
    NvidiaReleaseEvidence,
    evaluate_nvidia_release,
)
from javes_release.cli import run


SHA = "a" * 64
COMMIT = "b" * 40


def _ref(name: str, media_type: str = "application/json") -> dict:
    return {"name": name, "sha256": SHA, "media_type": media_type}


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
            "release_id": "nvidia-release-001",
            "git_commit": COMMIT,
            "release_version": "nvidia-deliverable-v1",
            "dataset_manifest_sha256": SHA,
            "site_bundle_sha256": SHA,
            "routing_params_sha256": SHA,
            "model_sha256": {"detector": SHA, "reid": SHA},
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


def test_complete_release_evidence_passes_deterministically() -> None:
    evidence = _evidence()
    first = evaluate_nvidia_release(evidence, _policy())
    second = evaluate_nvidia_release(evidence, _policy())
    assert first.status == "PASS"
    assert first == second
    assert first.failures == []


def test_quality_quarantine_and_target_tuning_block_release() -> None:
    evidence = _evidence()
    evidence.quality.quarantined_scenarios = 1
    evidence.quality.false_identity_rate = 0.33
    evidence.quality.target_specific_tuning = True
    report = evaluate_nvidia_release(evidence, _policy())
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

    blocked = evaluate_nvidia_release(evidence, _policy())
    assert "scope.external-components" in {
        item.check_id for item in blocked.failures
    }
    assert "workflow.clip-export" in {item.check_id for item in blocked.failures}

    evidence.scope.intermediate_delivery_approved = True
    evidence.scope.approval_reference = "delivery-decision-001"
    approved = evaluate_nvidia_release(evidence, _policy())
    assert approved.status == "PASS"


def test_missing_restart_and_failed_cold_suite_are_reported() -> None:
    evidence = _evidence()
    evidence.workflow = [
        item for item in evidence.workflow if item.name != "routing-restart"
    ]
    evidence.suites["cold-api-first-real-cv"].passed = False
    report = evaluate_nvidia_release(evidence, _policy())
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
    report = evaluate_nvidia_release(evidence, policy)
    assert {item.check_id for item in report.failures} >= {
        "operations.controls-api-latency",
        "operations.routing-latency",
        "operations.deletion-sla",
    }


def test_cli_writes_inspectable_json_and_html(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "report"
    evidence_path.write_text(
        json.dumps(_evidence().model_dump(mode="json")), encoding="utf-8"
    )
    policy_path.write_text(
        json.dumps(_policy().model_dump(mode="json")), encoding="utf-8"
    )
    report = run(evidence_path, output, policy_path)
    assert report.status == "PASS"
    assert json.loads((output / "report.json").read_text())["status"] == "PASS"
    assert "NVIDIA release sign-off" in (output / "index.html").read_text()


def test_cli_uses_built_in_policy_without_override(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    output = tmp_path / "report"
    evidence_path.write_text(
        json.dumps(_evidence().model_dump(mode="json")), encoding="utf-8"
    )
    assert run(evidence_path, output).status == "PASS"

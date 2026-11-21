"""Deterministic final gate for the NVIDIA-based JAVES-AI deliverable.

The gate deliberately consumes evidence instead of running services itself.  A
cold E2E, cached evaluation, operational rehearsal, and clean-environment
package job can therefore run independently and still produce one auditable
go/no-go decision.  Missing evidence is a failed check; it is never inferred
from a green unit-test run.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from pydantic import BaseModel
from javes_contracts.release import (
    AcceptancePolicy,
    ComponentDisposition,
    EvidenceBundleVerification,
    EvidenceRef,
    EvidenceStatus,
    GateCheck,
    LegacyNvidiaReleaseEvidence,
    NvidiaReleaseEvidence,
    NvidiaReleaseReport,
    VerifiedEvidenceFile,
)

REQUIRED_BLOCKS = tuple("ABCDEFGH")
REQUIRED_SUITES = (
    "fast-python-3.11",
    "fast-python-3.13",
    "cached-real-cv",
    "cold-api-first-real-cv",
    "clean-wheel-entrypoints",
)
REQUIRED_WORKFLOW = (
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
)
REQUIRED_ARTIFACT_MEDIA = (
    "text/html",
    "image/svg+xml",
    "application/json",
)

_ALLOWED_MEDIA = {
    "dataset": {"application/json"},
    "site": {"application/json", "application/zip", "application/gzip"},
    "params": {"application/json", "application/yaml", "text/yaml"},
    "model": {"application/octet-stream", "application/x-pytorch"},
    "report": {"application/json"},
    "inspectable": set(REQUIRED_ARTIFACT_MEDIA),
}
_JSON_SCHEMA_FIELDS = {
    "javes-release-evidence-document.v1": {"schema_version", "status"},
    "javes-real-cv-evaluation.v1": {"schema_version", "scenarios"},
    "javes-restore-rehearsal.v1": {"format", "status", "invariants"},
    "javes-audit-export.v1": {"format", "row_count", "sha256"},
    "javes-contract-bundle.v1": {"format", "files"},
    "javes-site-bundle.v1": {"format", "site_id", "version"},
    "javes-routing-params-bundle.v1": {"format", "version"},
    "javes-nvidia-site-bootstrap.v1": {"format"},
}


def evidence_references(
    evidence: NvidiaReleaseEvidence,
) -> list[tuple[str, EvidenceRef]]:
    return evidence.evidence_references()


def _manifest_sha256(evidence: NvidiaReleaseEvidence) -> str:
    return _canonical_sha256(evidence)


def _json_schema_valid(path: Path, reference: EvidenceRef) -> tuple[bool, str | None]:
    required = _JSON_SCHEMA_FIELDS.get(reference.schema_id or "")
    if required is None:
        return False, "UNKNOWN_JSON_SCHEMA"
    if reference.size_bytes > 16 * 1024 * 1024:
        return False, "JSON_SCHEMA_INPUT_TOO_LARGE"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "INVALID_JSON"
    if not isinstance(document, dict) or not required.issubset(document):
        return False, "SCHEMA_VALIDATION_FAILED"
    marker = document.get("schema_version", document.get("format"))
    if marker != reference.schema_id:
        return False, "SCHEMA_VALIDATION_FAILED"
    return True, None


def verify_evidence_bundle(
    evidence: NvidiaReleaseEvidence,
    bundle_root: str | Path,
    *,
    evidence_manifest_path: str | Path | None = None,
    checkout_commit: str | None = None,
) -> EvidenceBundleVerification:
    root = Path(bundle_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("evidence bundle root must be a directory")
    manifest_path = (
        Path(evidence_manifest_path).resolve(strict=True)
        if evidence_manifest_path is not None
        else None
    )
    verified: list[VerifiedEvidenceFile] = []
    for evidence_class, reference in evidence_references(evidence):
        failure: str | None = None
        computed_hash: str | None = None
        computed_size: int | None = None
        schema_valid: bool | None = None
        candidate = root.joinpath(*PurePosixPath(reference.path).parts)
        current = root
        for part in PurePosixPath(reference.path).parts:
            current = current / part
            if current.is_symlink():
                failure = "SYMLINK_NOT_ALLOWED"
                break
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, RuntimeError, ValueError):
            failure = failure or "MISSING_OR_ESCAPING_PATH"
            resolved = None
        if (
            resolved is not None
            and manifest_path is not None
            and resolved == manifest_path
        ):
            failure = "SELF_REFERENTIAL_EVIDENCE"
        if resolved is not None and failure is None:
            mode = os.stat(resolved, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                failure = "NOT_REGULAR_FILE"
        if (
            failure is None
            and reference.media_type not in _ALLOWED_MEDIA[evidence_class]
        ):
            failure = "MEDIA_TYPE_NOT_ALLOWED"
        if resolved is not None and failure is None:
            digest = hashlib.sha256()
            size = 0
            with resolved.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            computed_hash = digest.hexdigest()
            computed_size = size
            if size != reference.size_bytes:
                failure = "SIZE_MISMATCH"
            elif computed_hash != reference.sha256:
                failure = "SHA256_MISMATCH"
            elif reference.media_type == "application/json":
                schema_valid, schema_failure = _json_schema_valid(resolved, reference)
                failure = schema_failure
        verified.append(
            VerifiedEvidenceFile(
                name=reference.name,
                path=reference.path,
                media_type=reference.media_type,
                declared_sha256=reference.sha256,
                computed_sha256=computed_hash,
                declared_size_bytes=reference.size_bytes,
                computed_size_bytes=computed_size,
                schema_valid=schema_valid,
                passed=failure is None,
                failure_code=failure,
            )
        )
    git_verified = (
        None if checkout_commit is None else checkout_commit == evidence.git_commit
    )
    return EvidenceBundleVerification(
        manifest_sha256=_manifest_sha256(evidence),
        files=verified,
        git_commit_verified=git_verified,
        git_commit_failure_code=(
            "GIT_COMMIT_MISMATCH" if git_verified is False else None
        ),
    )


def _canonical_sha256(model: BaseModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evaluate_nvidia_release(
    evidence: NvidiaReleaseEvidence,
    policy: AcceptancePolicy,
    verification: EvidenceBundleVerification | None = None,
) -> NvidiaReleaseReport:
    """Evaluate policy only after the evidence bytes were independently verified."""

    checks: list[GateCheck] = []

    def add(check_id: str, category: str, passed: bool, detail: str) -> None:
        checks.append(
            GateCheck(
                check_id=check_id,
                category=category,
                passed=passed,
                detail=detail,
            )
        )

    add(
        "evidence.byte-verification",
        "evidence",
        bool(verification and verification.passed),
        (
            "all referenced bytes verified"
            if verification and verification.passed
            else "missing or failed byte verification"
        ),
    )
    if verification is not None:
        if verification.git_commit_verified is not None:
            add(
                "evidence.git-commit",
                "evidence",
                verification.git_commit_verified,
                (
                    "verified"
                    if verification.git_commit_verified
                    else (verification.git_commit_failure_code or "verification failed")
                ),
            )
        for item in verification.files:
            add(
                f"evidence.file.{item.name}",
                "evidence",
                item.passed,
                (
                    "verified"
                    if item.passed
                    else (item.failure_code or "verification failed")
                ),
            )

    unavailable = [
        name
        for name, disposition in (
            ("dashboard", evidence.scope.dashboard),
            ("clip-worker", evidence.scope.clip_worker),
        )
        if disposition == ComponentDisposition.UNAVAILABLE
    ]
    scope_approved = not unavailable or (
        evidence.scope.intermediate_delivery_approved
        and bool(evidence.scope.approval_reference)
    )
    add(
        "scope.external-components",
        "scope",
        scope_approved,
        (
            "all external components accepted"
            if not unavailable
            else (
                "unavailable components explicitly approved: " + ", ".join(unavailable)
                if scope_approved
                else "unavailable components require approval reference: "
                + ", ".join(unavailable)
            )
        ),
    )

    for block in REQUIRED_BLOCKS:
        item = evidence.blocks.get(block)
        passed = item is not None and item.status in {
            EvidenceStatus.PASS,
            EvidenceStatus.OUT_OF_SCOPE,
        }
        add(
            f"block.{block.lower()}",
            "blocks",
            passed,
            "missing block evidence" if item is None else item.status.value,
        )

    for suite in REQUIRED_SUITES:
        item = evidence.suites.get(suite)
        add(
            f"suite.{suite}",
            "suites",
            bool(item and item.passed),
            "missing suite evidence" if item is None else f"passed={item.passed}",
        )

    quality = evidence.quality
    quality_checks = {
        "scenarios": (
            quality.active_scenarios == policy.required_active_scenarios
            and quality.passed_scenarios == policy.required_active_scenarios,
            f"{quality.passed_scenarios}/{quality.active_scenarios} passed",
        ),
        "quarantine": (
            quality.quarantined_scenarios == 0,
            f"quarantined={quality.quarantined_scenarios}",
        ),
        "false-identity-rate": (
            quality.false_identity_rate <= policy.max_false_identity_rate,
            f"{quality.false_identity_rate:.6f} <= {policy.max_false_identity_rate:.6f}",
        ),
        "median-position": (
            quality.median_position_error_m <= policy.max_median_position_error_m,
            f"{quality.median_position_error_m:.6f} <= {policy.max_median_position_error_m:.6f} m",
        ),
        "p95-position": (
            quality.p95_position_error_m <= policy.max_p95_position_error_m,
            f"{quality.p95_position_error_m:.6f} <= {policy.max_p95_position_error_m:.6f} m",
        ),
        "hidden-containment": (
            quality.hidden_point_containment >= policy.min_hidden_point_containment,
            f"{quality.hidden_point_containment:.6f} >= {policy.min_hidden_point_containment:.6f}",
        ),
        "top2-branch": (
            quality.top2_branch_recall >= policy.min_top2_branch_recall,
            f"{quality.top2_branch_recall:.6f} >= {policy.min_top2_branch_recall:.6f}",
        ),
        "search-success": (
            quality.search_success_rate >= policy.min_search_success_rate,
            f"{quality.search_success_rate:.6f} >= {policy.min_search_success_rate:.6f}",
        ),
        "no-target-tuning": (
            not quality.target_specific_tuning,
            f"target_specific_tuning={quality.target_specific_tuning}",
        ),
    }
    for name, (passed, detail) in quality_checks.items():
        add(f"quality.{name}", "quality", passed, detail)

    operations = evidence.operations
    operation_checks = {
        "controls-api-latency": (
            operations.controls_api_latency_p95_s,
            policy.max_controls_api_latency_p95_s,
        ),
        "routing-latency": (
            operations.routing_latency_p95_s,
            policy.max_routing_latency_p95_s,
        ),
        "deletion-sla": (operations.deletion_sla_p95_s, policy.max_deletion_sla_p95_s),
    }
    for name, (actual, maximum) in operation_checks.items():
        add(
            f"operations.{name}",
            "operations",
            actual <= maximum,
            f"{actual:.6f} <= {maximum:.6f}",
        )
    add(
        "operations.cv-throughput-measured",
        "operations",
        True,
        (
            f"search_p95_s={operations.search_time_p95_s:.6f}; "
            f"camera_minutes={operations.mean_camera_minutes:.6f}; "
            f"queue_p95_s={operations.queue_latency_p95_s:.6f}; "
            f"worker_p95_s={operations.worker_latency_p95_s:.6f}; "
            f"retry_rate={operations.retry_rate:.6f}"
        ),
    )
    for name, passed in (
        ("audit-complete", operations.audit_complete),
        ("cold-warm-equal", operations.cold_warm_business_equal),
        ("restart-repeatable", operations.restart_repeatable),
    ):
        add(f"operations.{name}", "operations", passed, f"passed={passed}")

    workflow = {item.name: item for item in evidence.workflow}
    for name in REQUIRED_WORKFLOW:
        item = workflow.get(name)
        passed = bool(item and item.status == EvidenceStatus.PASS)
        if item and item.status == EvidenceStatus.WAIVED:
            passed = (
                name == "clip-export"
                and evidence.scope.clip_worker == ComponentDisposition.UNAVAILABLE
                and scope_approved
            )
        add(
            f"workflow.{name}",
            "workflow",
            passed,
            "missing workflow evidence" if item is None else item.status.value,
        )

    media_types = {item.media_type for item in evidence.inspectable_artifacts}
    for media_type in REQUIRED_ARTIFACT_MEDIA:
        add(
            f"artifacts.{media_type}",
            "artifacts",
            media_type in media_types,
            "present" if media_type in media_types else "missing inspectable artifact",
        )
    add(
        "artifacts.models",
        "artifacts",
        bool(evidence.models),
        f"model files={len(evidence.models)}",
    )

    status: Literal["PASS", "BLOCKED"] = (
        "PASS" if all(item.passed for item in checks) else "BLOCKED"
    )
    return NvidiaReleaseReport(
        release_id=evidence.release_id,
        release_version=evidence.release_version,
        git_commit=evidence.git_commit,
        status=status,
        evidence_sha256=(
            verification.manifest_sha256
            if verification is not None
            else _canonical_sha256(evidence)
        ),
        policy_sha256=_canonical_sha256(policy),
        verification=verification,
        checks=checks,
    )


def legacy_failure_report(
    evidence: LegacyNvidiaReleaseEvidence,
    policy: AcceptancePolicy,
) -> NvidiaReleaseReport:
    return NvidiaReleaseReport(
        release_id=evidence.release_id,
        release_version=evidence.release_version,
        git_commit=evidence.git_commit,
        status="BLOCKED",
        evidence_sha256=hashlib.sha256(
            json.dumps(
                evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        policy_sha256=_canonical_sha256(policy),
        checks=[
            GateCheck(
                check_id="evidence.legacy",
                category="evidence",
                passed=False,
                detail="UNVERIFIED_LEGACY",
            )
        ],
    )

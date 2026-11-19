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
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComponentDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIVED = "WAIVED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class EvidenceRef(StrictModel):
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1)


class DeliveryScope(StrictModel):
    dashboard: ComponentDisposition
    clip_worker: ComponentDisposition
    intermediate_delivery_approved: bool = False
    approval_reference: str | None = None


class BlockEvidence(StrictModel):
    status: EvidenceStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _explain_non_pass(self) -> BlockEvidence:
        if self.status != EvidenceStatus.PASS and not self.reason:
            raise ValueError("non-PASS block evidence requires a reason")
        return self


class SuiteEvidence(StrictModel):
    passed: bool
    evidence: EvidenceRef


class QualityEvidence(StrictModel):
    active_scenarios: int = Field(ge=0)
    passed_scenarios: int = Field(ge=0)
    quarantined_scenarios: int = Field(ge=0)
    identity_precision: float = Field(ge=0.0, le=1.0)
    identity_recall: float = Field(ge=0.0, le=1.0)
    false_identity_rate: float = Field(ge=0.0, le=1.0)
    tracklet_precision: float = Field(ge=0.0, le=1.0)
    tracklet_recall: float = Field(ge=0.0, le=1.0)
    median_position_error_m: float = Field(ge=0.0)
    p95_position_error_m: float = Field(ge=0.0)
    hidden_point_containment: float = Field(ge=0.0, le=1.0)
    top2_branch_recall: float = Field(ge=0.0, le=1.0)
    search_success_rate: float = Field(ge=0.0, le=1.0)
    target_specific_tuning: bool


class OperationalEvidence(StrictModel):
    controls_api_latency_p95_s: float = Field(ge=0.0)
    search_time_p95_s: float = Field(ge=0.0)
    mean_camera_minutes: float = Field(ge=0.0)
    queue_latency_p95_s: float = Field(ge=0.0)
    worker_latency_p95_s: float = Field(ge=0.0)
    routing_latency_p95_s: float = Field(ge=0.0)
    retry_rate: float = Field(ge=0.0, le=1.0)
    deletion_sla_p95_s: float = Field(ge=0.0)
    audit_complete: bool
    cold_warm_business_equal: bool
    restart_repeatable: bool


class WorkflowCheck(StrictModel):
    name: str = Field(min_length=1)
    status: EvidenceStatus
    evidence: EvidenceRef
    reason: str | None = None

    @model_validator(mode="after")
    def _explain_exception(self) -> WorkflowCheck:
        if self.status != EvidenceStatus.PASS and not self.reason:
            raise ValueError("non-PASS workflow check requires a reason")
        return self


class NvidiaReleaseEvidence(StrictModel):
    schema_version: Literal["javes-nvidia-release-evidence.v1"] = (
        "javes-nvidia-release-evidence.v1"
    )
    release_id: str = Field(min_length=1)
    git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    release_version: str = Field(min_length=1)
    dataset_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    site_bundle_sha256: str = Field(pattern=SHA256_PATTERN)
    routing_params_sha256: str = Field(pattern=SHA256_PATTERN)
    model_sha256: dict[str, str]
    profile_fingerprint: str = Field(pattern=SHA256_PATTERN)
    scope: DeliveryScope
    blocks: dict[str, BlockEvidence]
    suites: dict[str, SuiteEvidence]
    quality: QualityEvidence
    operations: OperationalEvidence
    workflow: list[WorkflowCheck]
    inspectable_artifacts: list[EvidenceRef]

    @model_validator(mode="after")
    def _unique_evidence(self) -> NvidiaReleaseEvidence:
        workflow_names = [item.name for item in self.workflow]
        if len(workflow_names) != len(set(workflow_names)):
            raise ValueError("workflow check names must be unique")
        artifact_names = [item.name for item in self.inspectable_artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("inspectable artifact names must be unique")
        for name, checksum in self.model_sha256.items():
            if not name or len(checksum) != 64 or any(
                character not in "0123456789abcdef" for character in checksum
            ):
                raise ValueError("model_sha256 must map names to lowercase SHA256")
        return self


class AcceptancePolicy(StrictModel):
    schema_version: Literal["javes-nvidia-acceptance-policy.v1"] = (
        "javes-nvidia-acceptance-policy.v1"
    )
    required_active_scenarios: int = Field(default=12, ge=1)
    max_false_identity_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    max_median_position_error_m: float = Field(default=0.75, gt=0.0)
    max_p95_position_error_m: float = Field(default=1.5, gt=0.0)
    min_hidden_point_containment: float = Field(default=0.90, ge=0.0, le=1.0)
    min_top2_branch_recall: float = Field(default=0.90, ge=0.0, le=1.0)
    min_search_success_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    max_controls_api_latency_p95_s: float = Field(default=2.0, gt=0.0)
    max_routing_latency_p95_s: float = Field(default=2.0, gt=0.0)
    max_deletion_sla_p95_s: float = Field(default=60.0, gt=0.0)


class GateCheck(StrictModel):
    check_id: str
    category: str
    passed: bool
    detail: str


class NvidiaReleaseReport(StrictModel):
    schema_version: Literal["javes-nvidia-release-report.v1"] = (
        "javes-nvidia-release-report.v1"
    )
    release_id: str
    release_version: str
    git_commit: str
    status: Literal["PASS", "BLOCKED"]
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    policy_sha256: str = Field(pattern=SHA256_PATTERN)
    checks: list[GateCheck]

    @property
    def failures(self) -> list[GateCheck]:
        return [item for item in self.checks if not item.passed]


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
) -> NvidiaReleaseReport:
    """Evaluate one immutable evidence bundle without reading external state."""

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
            else "unavailable components explicitly approved: " + ", ".join(unavailable)
            if scope_approved
            else "unavailable components require approval reference: "
            + ", ".join(unavailable)
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
        "routing-latency": (operations.routing_latency_p95_s, policy.max_routing_latency_p95_s),
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
        bool(evidence.model_sha256),
        f"model checksums={len(evidence.model_sha256)}",
    )

    status: Literal["PASS", "BLOCKED"] = (
        "PASS" if all(item.passed for item in checks) else "BLOCKED"
    )
    return NvidiaReleaseReport(
        release_id=evidence.release_id,
        release_version=evidence.release_version,
        git_commit=evidence.git_commit,
        status=status,
        evidence_sha256=_canonical_sha256(evidence),
        policy_sha256=_canonical_sha256(policy),
        checks=checks,
    )

"""Canonical contracts for byte-verified NVIDIA release evidence."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .routing import ContractModel


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ComponentDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIVED = "WAIVED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class EvidenceRef(ContractModel):
    name: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1)
    schema_id: str | None = None

    @model_validator(mode="after")
    def _safe_bundle_path(self) -> EvidenceRef:
        candidate = PurePosixPath(self.path)
        if (
            candidate.is_absolute()
            or "\\" in self.path
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError("evidence path must be a safe bundle-relative POSIX path")
        if self.media_type == "application/json" and not self.schema_id:
            raise ValueError("JSON evidence requires schema_id")
        if self.media_type != "application/json" and self.schema_id is not None:
            raise ValueError("schema_id is only valid for JSON evidence")
        return self


class DeliveryScope(ContractModel):
    dashboard: ComponentDisposition
    clip_worker: ComponentDisposition
    intermediate_delivery_approved: bool = False
    approval_reference: str | None = None


class BlockEvidence(ContractModel):
    status: EvidenceStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _explain_non_pass(self) -> BlockEvidence:
        if self.status != EvidenceStatus.PASS and not self.reason:
            raise ValueError("non-PASS block evidence requires a reason")
        return self


class SuiteEvidence(ContractModel):
    passed: bool
    evidence: EvidenceRef


class QualityEvidence(ContractModel):
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


class OperationalEvidence(ContractModel):
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


class WorkflowCheck(ContractModel):
    name: str = Field(min_length=1)
    status: EvidenceStatus
    evidence: EvidenceRef
    reason: str | None = None

    @model_validator(mode="after")
    def _explain_exception(self) -> WorkflowCheck:
        if self.status != EvidenceStatus.PASS and not self.reason:
            raise ValueError("non-PASS workflow check requires a reason")
        return self


class NvidiaReleaseEvidence(ContractModel):
    schema_version: Literal["javes-nvidia-release-evidence.v2"] = (
        "javes-nvidia-release-evidence.v2"
    )
    release_id: str = Field(min_length=1)
    git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    release_version: str = Field(min_length=1)
    dataset_manifest: EvidenceRef
    site_bundle: EvidenceRef
    routing_params: EvidenceRef
    models: dict[str, EvidenceRef]
    profile_fingerprint: str = Field(pattern=SHA256_PATTERN)
    scope: DeliveryScope
    blocks: dict[str, BlockEvidence]
    suites: dict[str, SuiteEvidence]
    quality: QualityEvidence
    operations: OperationalEvidence
    workflow: list[WorkflowCheck]
    inspectable_artifacts: list[EvidenceRef]

    def evidence_references(self) -> list[tuple[str, EvidenceRef]]:
        references: list[tuple[str, EvidenceRef]] = [
            ("dataset", self.dataset_manifest),
            ("site", self.site_bundle),
            ("params", self.routing_params),
        ]
        references.extend(("model", item) for _, item in sorted(self.models.items()))
        references.extend(
            ("report", item)
            for _, block in sorted(self.blocks.items())
            for item in block.evidence
        )
        references.extend(
            ("report", suite.evidence) for _, suite in sorted(self.suites.items())
        )
        references.extend(("report", item.evidence) for item in self.workflow)
        references.extend(("inspectable", item) for item in self.inspectable_artifacts)
        return references

    @model_validator(mode="after")
    def _unique_evidence(self) -> NvidiaReleaseEvidence:
        workflow_names = [item.name for item in self.workflow]
        if len(workflow_names) != len(set(workflow_names)):
            raise ValueError("workflow check names must be unique")
        artifact_names = [item.name for item in self.inspectable_artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("inspectable artifact names must be unique")
        if not self.models or any(not name for name in self.models):
            raise ValueError("models must map non-empty names to evidence files")
        references = [item for _, item in self.evidence_references()]
        paths = [item.path for item in references]
        names = [item.name for item in references]
        if len(paths) != len(set(paths)):
            raise ValueError("evidence paths must be globally unique")
        if len(names) != len(set(names)):
            raise ValueError("evidence names must be globally unique")
        return self


class LegacyNvidiaReleaseEvidence(ContractModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal["javes-nvidia-release-evidence.v1"]
    release_id: str
    git_commit: str
    release_version: str


class VerifiedEvidenceFile(ContractModel):
    name: str
    path: str
    media_type: str
    declared_sha256: str
    computed_sha256: str | None = None
    declared_size_bytes: int
    computed_size_bytes: int | None = None
    schema_valid: bool | None = None
    passed: bool
    failure_code: str | None = None


class EvidenceBundleVerification(ContractModel):
    schema_version: Literal["javes-evidence-verification.v1"] = (
        "javes-evidence-verification.v1"
    )
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    files: list[VerifiedEvidenceFile]
    git_commit_verified: bool | None = None
    git_commit_failure_code: str | None = None

    @property
    def passed(self) -> bool:
        return (
            all(item.passed for item in self.files)
            and self.git_commit_verified is not False
        )


class AcceptancePolicy(ContractModel):
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


class GateCheck(ContractModel):
    check_id: str
    category: str
    passed: bool
    detail: str


class NvidiaReleaseReport(ContractModel):
    schema_version: Literal["javes-nvidia-release-report.v2"] = (
        "javes-nvidia-release-report.v2"
    )
    release_id: str
    release_version: str
    git_commit: str
    status: Literal["PASS", "BLOCKED"]
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    policy_sha256: str = Field(pattern=SHA256_PATTERN)
    verification: EvidenceBundleVerification | None = None
    checks: list[GateCheck]

    @property
    def failures(self) -> list[GateCheck]:
        return [item for item in self.checks if not item.passed]

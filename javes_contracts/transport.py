"""Versioned HTTP transport contracts for component boundaries.

The business DTOs remain in :mod:`javes_contracts.boundary`.  These envelopes
add delivery, leasing and immutable configuration semantics without leaking
Main Infra persistence models to workers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from routing.contracts import (
    Anchors,
    CaseInfo,
    ContractModel,
    ProbeResult,
    RoutingWorkerResult,
    SearchState,
    SiteGraph,
)

from .boundary import RetentionClass, aware_utc


WorkerComponent = Literal["perception", "clip", "retention"]
WorkerOperation = Literal[
    "GALLERY_BUILD",
    "PROBE",
    "PERCEPTION_CANCEL",
    "CLIP_CUT",
    "CLIP_STATE",
    "CLIP_EXPORT",
    "RETENTION_REGISTER",
    "RETENTION_DELETE",
]


class ArtifactManifest(ContractModel):
    """One product-owned object in the shared private filesystem."""

    contract_version: Literal["artifact-manifest.v1"] = "artifact-manifest.v1"
    object_key: str
    kind: Literal[
        "crop",
        "embedding",
        "gallery",
        "live_jpeg",
        "clip",
        "route_svg",
        "export",
    ]
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    retention_class: Literal["ephemeral", "case_14d", "review_hold"] = "case_14d"
    mime_type: str | None = None

    @field_validator("object_key")
    @classmethod
    def _safe_object_key(cls, value: str) -> str:
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("object_key must be a safe relative POSIX path")
        return value

    @field_validator("created_at", mode="after")
    @classmethod
    def _aware_created_at(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    def policy_class(self) -> RetentionClass:
        return {
            "ephemeral": RetentionClass.EPHEMERAL,
            "case_14d": RetentionClass.CASE_RESULT,
            "review_hold": RetentionClass.REVIEW_HOLD,
        }[self.retention_class]


class WorkerClaimRequest(ContractModel):
    contract_version: Literal["worker-claim.v1"] = "worker-claim.v1"
    worker_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=1, ge=1, le=100)
    lease_seconds: int = Field(default=30, ge=5, le=600)


class WorkerTaskEnvelope(ContractModel):
    contract_version: Literal["worker-task.v1"] = "worker-task.v1"
    task_id: str
    case_id: str
    component: WorkerComponent
    operation: WorkerOperation
    attempt: int = Field(ge=1)
    lease_token: str
    lease_expires_at: datetime
    deadline: datetime | None = None
    payload: dict[str, Any]

    @field_validator("lease_expires_at", "deadline", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value)


class WorkerClaimResponse(ContractModel):
    contract_version: Literal["worker-claim-result.v1"] = "worker-claim-result.v1"
    tasks: list[WorkerTaskEnvelope] = Field(default_factory=list)


class WorkerHeartbeatRequest(ContractModel):
    contract_version: Literal["worker-heartbeat.v1"] = "worker-heartbeat.v1"
    lease_token: str
    extend_seconds: int = Field(default=30, ge=5, le=600)


class WorkerHeartbeatResponse(ContractModel):
    contract_version: Literal["worker-heartbeat-result.v1"] = "worker-heartbeat-result.v1"
    task_id: str
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False

    @field_validator("lease_expires_at", mode="after")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value)


class WorkerTaskResultRequest(ContractModel):
    contract_version: Literal["worker-task-result.v1"] = "worker-task-result.v1"
    lease_token: str
    result: dict[str, Any] | None = None
    artifacts: list[ArtifactManifest] = Field(default_factory=list)


class WorkerTaskFailureRequest(ContractModel):
    contract_version: Literal["worker-task-failure.v1"] = "worker-task-failure.v1"
    lease_token: str
    error_code: str
    detail: str | None = None
    retryable: bool = True


class WorkerTaskAck(ContractModel):
    contract_version: Literal["worker-task-ack.v1"] = "worker-task-ack.v1"
    task_id: str
    accepted: bool = True
    duplicate: bool = False
    terminal: bool = True


class RoutingInitRequest(ContractModel):
    contract_version: Literal["routing-init.v1"] = "routing-init.v1"
    case: CaseInfo
    anchors: Anchors
    site_id: str
    site_version: str
    params_version: str
    now: datetime

    @field_validator("now", mode="after")
    @classmethod
    def _aware_now(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class RoutingInitResponse(ContractModel):
    contract_version: Literal["routing-init-result.v1"] = "routing-init-result.v1"
    state: SearchState
    site_checksum: str
    params_checksum: str


class RoutingStepRequest(ContractModel):
    contract_version: Literal["routing-step.v1"] = "routing-step.v1"
    state: SearchState
    probe_results: list[ProbeResult] = Field(default_factory=list)
    anchors: Anchors = Field(default_factory=Anchors)
    site_id: str
    site_version: str
    params_version: str
    now: datetime

    @field_validator("now", mode="after")
    @classmethod
    def _aware_now(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class RoutingStepResponse(ContractModel):
    contract_version: Literal["routing-step-result.v1"] = "routing-step-result.v1"
    result: RoutingWorkerResult
    site_checksum: str
    params_checksum: str


class SiteAssetDescriptor(ContractModel):
    asset_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str


class SiteVersionResponse(ContractModel):
    contract_version: Literal["site-config.v1"] = "site-config.v1"
    site_id: str
    version: str
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph: SiteGraph
    assets: list[SiteAssetDescriptor] = Field(default_factory=list)


class RoutingParamsResponse(ContractModel):
    contract_version: Literal["routing-params.v1"] = "routing-params.v1"
    version: str
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    params: dict[str, Any]

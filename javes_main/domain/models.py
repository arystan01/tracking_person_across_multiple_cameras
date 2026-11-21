"""Main Infra domain state, independent of HTTP and persistence frameworks."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from javes_contracts import (
    Anchors,
    CaseLifecycle,
    CaseSummary as CaseSummary,
    CaseView as CaseView,
    ClipRecord,
    CreateCaseBody,
    LiveFeedUpdate,
    PerceptionProbeJob,
    ProbeResult,
    RenderedRouteBundle,
    RouteMapView as RouteMapView,
    SearchState,
    StepResult,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class JobState(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"


class OutboxState(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RoutingTriggerState(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    FAILED = "FAILED"


class ReferenceImageUploadStatus(str, Enum):
    READY = "READY"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class ReferenceImageUploadRecord(DomainModel):
    upload_id: str
    case_id: str
    operator_id: str
    object_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    status: ReferenceImageUploadStatus = ReferenceImageUploadStatus.READY
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @field_validator("created_at", "expires_at", "consumed_at", mode="after")
    @classmethod
    def _aware_upload_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class OutboxTopic(str, Enum):
    TARGET_GALLERY = "TARGET_GALLERY"
    PERCEPTION_CANCEL = "PERCEPTION_CANCEL"
    CLIP_CUT = "CLIP_CUT"
    CLIP_STATE = "CLIP_STATE"
    CLIP_EXPORT = "CLIP_EXPORT"
    RETENTION_REGISTER = "RETENTION_REGISTER"
    RETENTION_COMMAND = "RETENTION_COMMAND"
    RETENTION_HOLD = "RETENTION_HOLD"


class CaseDataKind(str, Enum):
    ROUTING_STATE = "ROUTING_STATE"
    ROUTING_RUN = "ROUTING_RUN"
    ROUTING_INPUT = "ROUTING_INPUT"
    ROUTE_REVISION = "ROUTE_REVISION"
    PROBE_REQUEST = "PROBE_REQUEST"
    TRACKLET = "TRACKLET"
    CANDIDATE = "CANDIDATE"
    OPERATOR_DECISION = "OPERATOR_DECISION"
    OPERATOR_DECISION_REVOCATION = "OPERATOR_DECISION_REVOCATION"
    LEGAL_HOLD = "LEGAL_HOLD"
    LEGAL_HOLD_RELEASE = "LEGAL_HOLD_RELEASE"
    ARTIFACT = "ARTIFACT"


class CaseDataRecord(DomainModel):
    record_id: str
    case_id: str
    kind: CaseDataKind
    schema_version: str = "case-data.v1"
    occurred_at: datetime
    payload: dict[str, Any]

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]


class UserRole(str, Enum):
    ROOT = "ROOT"
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"
    SERVICE = "SERVICE"


ROLE_PERMISSIONS: dict[UserRole, frozenset[str]] = {
    UserRole.ROOT: frozenset({"*"}),
    UserRole.ADMIN: frozenset(
        {
            "case.read",
            "case.create",
            "case.review",
            "case.close",
            "case.export",
            "case.delete",
            "case.events.read",
            "audit.read",
        }
    ),
    UserRole.OPERATOR: frozenset(
        {
            "case.read",
            "case.create",
            "case.review",
            "case.close",
            "case.export",
            "case.events.read",
        }
    ),
    UserRole.VIEWER: frozenset({"case.read"}),
    UserRole.SERVICE: frozenset({"worker.result.write"}),
}


class UserAccount(DomainModel):
    user_id: str
    username: str = Field(min_length=3, max_length=128)
    password_hash: str
    role: UserRole
    active: bool = True
    must_change_password: bool = True
    failed_login_count: int = Field(default=0, ge=0)
    locked_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None

    @field_validator(
        "created_at", "updated_at", "locked_until", "disabled_at", mode="after"
    )
    @classmethod
    def _aware_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class AuthSession(DomainModel):
    session_id: str
    token_hash: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @field_validator("created_at", "expires_at", "revoked_at", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class AuditEvent(DomainModel):
    event_id: str
    case_id: str
    event_type: str
    actor_id: str | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]


class LegalHoldRecord(DomainModel):
    hold_id: str
    case_id: str
    external_ticket_id: str
    reason: str
    set_by: str
    set_at: datetime
    active: bool = True
    release_pending: bool = False
    released_by: str | None = None
    released_at: datetime | None = None
    release_id: str | None = None
    release_reason: str | None = None

    @field_validator("set_at", "released_at", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class JobRecord(DomainModel):
    job_id: str
    case_id: str
    kind: str = "PERCEPTION"
    state: JobState = JobState.PENDING
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    dedupe_key: str
    payload: PerceptionProbeJob
    result_fingerprints: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None

    @field_validator("created_at", "updated_at", "lease_expires_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class OutboxRecord(DomainModel):
    message_id: str
    case_id: str
    topic: str
    dedupe_key: str
    payload: dict[str, Any]
    state: OutboxState = OutboxState.PENDING
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None

    @field_validator(
        "available_at", "created_at", "updated_at", "lease_expires_at", mode="after"
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class ProbeResultRecord(DomainModel):
    fingerprint: str
    job_id: str
    case_id: str
    result: ProbeResult
    accepted_at: datetime

    @field_validator("accepted_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]


class RoutingTriggerRecord(DomainModel):
    trigger_id: str
    case_id: str
    dedupe_key: str
    trigger_type: str
    probe_results: list[ProbeResult] = Field(default_factory=list)
    anchors: Anchors = Field(default_factory=Anchors)
    state: RoutingTriggerState = RoutingTriggerState.PENDING
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None

    @field_validator("created_at", "updated_at", "lease_expires_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class StoredRoute(DomainModel):
    revision: int = Field(ge=0)
    step_result: StepResult
    rendered: RenderedRouteBundle
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]


class CaseRecord(DomainModel):
    request: CreateCaseBody
    params_version: str = "1"
    lifecycle: CaseLifecycle = CaseLifecycle.PREPARING
    revision: int = Field(default=0, ge=0)
    routing_state: SearchState | None = None
    latest_route: StoredRoute | None = None
    gallery_ref: str | None = None
    gallery_model_version: str | None = None
    pending_confirm_tracklet_ids: list[str] = Field(default_factory=list)
    pending_reject_tracklet_ids: list[str] = Field(default_factory=list)
    tracklets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    clips: dict[str, ClipRecord] = Field(default_factory=dict)
    candidate_to_clip: dict[str, str] = Field(default_factory=dict)
    export_ref: dict[str, Any] | None = None
    live_feed: LiveFeedUpdate | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    retention_due_at: datetime | None = None
    deleted_at: datetime | None = None
    next_heartbeat_at: datetime | None = None
    content_redacted: bool = False
    replay_capability: str = "INCOMPLETE"
    legal_holds: dict[str, LegalHoldRecord] = Field(default_factory=dict)
    pending_retention_action: str | None = None

    @field_validator(
        "created_at",
        "updated_at",
        "closed_at",
        "retention_due_at",
        "deleted_at",
        "next_heartbeat_at",
        mode="after",
    )
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

"""Versioned worker and Controls boundary contracts owned by Main Infra."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from routing.contracts import ContractModel, ProbeRequest


def aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


class ArtifactRef(ContractModel):
    """Stable storage/archive identity, never a short-lived playback URL."""

    archive_id: str | None = None
    object_key: str | None = None
    uri: str | None = None

    @model_validator(mode="after")
    def _has_identity(self) -> ArtifactRef:
        if not any((self.archive_id, self.object_key, self.uri)):
            raise ValueError("artifact reference needs archive_id, object_key, or uri")
        return self

    def identity(self) -> str:
        return self.archive_id or self.object_key or self.uri or ""


class VideoAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class VideoSegmentRequest(ContractModel):
    contract_version: Literal["video-archive-request.v1"] = "video-archive-request.v1"
    request_id: str
    camera_id: str
    t_from: datetime
    t_to: datetime | Literal["OPEN"]

    @field_validator("t_from", mode="after")
    @classmethod
    def _aware_from(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    @field_validator("t_to", mode="after")
    @classmethod
    def _aware_to(cls, value: datetime | str) -> datetime | str:
        return aware_utc(value) if isinstance(value, datetime) else value

    @model_validator(mode="after")
    def _ordered_window(self) -> VideoSegmentRequest:
        if isinstance(self.t_to, datetime) and self.t_to < self.t_from:
            raise ValueError("video segment t_to must not precede t_from")
        return self


class VideoSegmentResult(ContractModel):
    contract_version: Literal["video-archive-result.v1"] = "video-archive-result.v1"
    request_id: str
    camera_id: str
    availability: VideoAvailability
    requested_from: datetime
    requested_to: datetime | Literal["OPEN"]
    covered_from: datetime | None = None
    covered_to: datetime | None = None
    coverage_ratio: float = Field(ge=0.0, le=1.0)
    source_ref: ArtifactRef | None = None
    source_clock: Literal["UTC_CORRECTED"] = "UTC_CORRECTED"
    error_code: str | None = None

    @field_validator(
        "requested_from", "requested_to", "covered_from", "covered_to", mode="after"
    )
    @classmethod
    def _aware_times(cls, value: datetime | str | None):
        return aware_utc(value) if isinstance(value, datetime) else value


class LiveFeedUpdate(ContractModel):
    """Latest authorised live-view metadata emitted by Perception."""

    contract_version: Literal["live-feed-update.v1"] = "live-feed-update.v1"
    update_id: str
    case_id: str
    camera_id: str
    tracklet_id: str
    bbox: tuple[float, float, float, float]
    observed_at: datetime
    stream_ref: str
    stream_kind: Literal["JPEG", "MJPEG", "HLS"] = "JPEG"
    expires_at: datetime | None = None

    @field_validator("observed_at", "expires_at", mode="after")
    @classmethod
    def _aware_live_times(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value)

    @model_validator(mode="after")
    def _expiry_after_observation(self) -> LiveFeedUpdate:
        if self.expires_at is not None and self.expires_at < self.observed_at:
            raise ValueError("live feed expires_at must not precede observed_at")
        return self


class TargetGalleryCommand(ContractModel):
    contract_version: Literal["target-gallery.v1"] = "target-gallery.v1"
    command_id: str
    case_id: str
    source: Literal["reference", "confirmed_tracklet"]
    image_ref: ArtifactRef | None = None
    tracklet_id: str | None = None
    model_version: str = "fake-reid-v1"

    @model_validator(mode="after")
    def _source_payload(self) -> TargetGalleryCommand:
        if self.source == "reference" and self.image_ref is None:
            raise ValueError("reference gallery command requires image_ref")
        if self.source == "confirmed_tracklet" and not self.tracklet_id:
            raise ValueError("confirmed_tracklet command requires tracklet_id")
        return self


class TargetGalleryResult(ContractModel):
    contract_version: Literal["target-gallery-result.v1"] = "target-gallery-result.v1"
    command_id: str
    case_id: str
    gallery_ref: str
    source_count: int = Field(ge=1)
    model_version: str


class PerceptionProbeJob(ContractModel):
    contract_version: Literal["perception-job.v1"] = "perception-job.v1"
    job_id: str
    case_id: str
    kind: Literal["PROBE"] = "PROBE"
    target_gallery_ref: str
    probe: ProbeRequest
    deadline: datetime | None = None

    @field_validator("deadline", mode="after")
    @classmethod
    def _aware_deadline(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value)


class FakeObservation(ContractModel):
    observation_id: str
    camera_id: str
    t_start: datetime
    t_end: datetime
    appearance_score: float = Field(ge=0.0, le=1.0)
    bbox_first: tuple[float, float, float, float] = (0.0, 0.0, 10.0, 10.0)
    bbox_last: tuple[float, float, float, float] = (10.0, 10.0, 10.0, 10.0)
    cosine: float | None = None
    association_id: str | None = None
    clip_ref: ArtifactRef | None = None

    @field_validator("t_start", "t_end", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _ordered(self) -> FakeObservation:
        if self.t_end < self.t_start:
            raise ValueError("observation t_end must not precede t_start")
        return self


class ClipProcessingStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class ClipLifecycleState(str, Enum):
    HOLD = "HOLD"
    CONFIRMED = "CONFIRMED"
    RELEASED = "RELEASED"


class CutClipCommand(ContractModel):
    contract_version: Literal["clip-command.v1"] = "clip-command.v1"
    clip_request_id: str
    case_id: str
    candidate_id: str
    tracklet_id: str
    camera_id: str
    t_from: datetime
    t_to: datetime
    padding_before_s: float = Field(default=0.0, ge=0.0)
    padding_after_s: float = Field(default=0.0, ge=0.0)
    source_ref: ArtifactRef
    initial_state: Literal["HOLD"] = "HOLD"

    @field_validator("t_from", "t_to", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _ordered(self) -> CutClipCommand:
        if self.t_to < self.t_from:
            raise ValueError("clip t_to must not precede t_from")
        return self


class ClipRecord(ContractModel):
    contract_version: Literal["clip-record.v1"] = "clip-record.v1"
    clip_request_id: str
    clip_id: str
    case_id: str
    candidate_id: str
    tracklet_id: str
    status: ClipProcessingStatus
    state: ClipLifecycleState | None = None
    camera_id: str
    t_start: datetime
    t_end: datetime
    mime_type: str = "video/mp4"
    duration_s: float = Field(ge=0.0)
    artifact_ref: ArtifactRef | None = None
    checksum: str | None = None
    error_code: str | None = None
    revision: int = Field(default=1, ge=1)

    @field_validator("t_start", "t_end", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class ClipStateCommand(ContractModel):
    contract_version: Literal["clip-state-command.v1"] = "clip-state-command.v1"
    command_id: str
    case_id: str
    clip_id: str
    action: Literal["HOLD", "CONFIRM", "RELEASE"]
    operator_id: str | None = None
    occurred_at: datetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware_occurred(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class ExportClipsCommand(ContractModel):
    contract_version: Literal["clip-export.v1"] = "clip-export.v1"
    export_request_id: str
    case_id: str
    ordered_clip_ids: list[str]
    format: Literal["video/mp4"] = "video/mp4"

    @model_validator(mode="after")
    def _nonempty_unique(self) -> ExportClipsCommand:
        if not self.ordered_clip_ids:
            raise ValueError("ordered_clip_ids must not be empty")
        if len(set(self.ordered_clip_ids)) != len(self.ordered_clip_ids):
            raise ValueError("ordered_clip_ids must be unique")
        return self


class ExportClipsResult(ContractModel):
    contract_version: Literal["clip-export-result.v1"] = "clip-export-result.v1"
    export_request_id: str
    case_id: str
    status: ClipProcessingStatus
    ordered_clip_ids: list[str]
    missing_clip_ids: list[str] = Field(default_factory=list)
    artifact_ref: ArtifactRef | None = None
    checksum: str | None = None
    duration_s: float = Field(default=0.0, ge=0.0)
    error_code: str | None = None


class RetentionClass(str, Enum):
    EPHEMERAL = "EPHEMERAL"
    REVIEW_HOLD = "REVIEW_HOLD"
    CASE_RESULT = "CASE_RESULT"
    CONFIRMED_EVIDENCE = "CONFIRMED_EVIDENCE"
    AUDIT = "AUDIT"


class RetentionResource(ContractModel):
    contract_version: Literal["retention-resource.v1"] = "retention-resource.v1"
    resource_id: str
    case_id: str
    kind: str
    retention_class: RetentionClass
    artifact_ref: ArtifactRef | None = None
    expires_at: datetime | None = None
    legal_hold: bool = False
    deleted: bool = False

    @field_validator("expires_at", mode="after")
    @classmethod
    def _aware_expiry(cls, value: datetime | None) -> datetime | None:
        return aware_utc(value)


class RetentionCommand(ContractModel):
    contract_version: Literal["retention-command.v1"] = "retention-command.v1"
    command_id: str
    case_id: str
    action: Literal["CLOSE_CASE", "DELETE_CASE", "TTL_EXPIRED"]
    actor_id: str | None = None
    occurred_at: datetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware_occurred(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class RetentionResult(ContractModel):
    contract_version: Literal["retention-result.v1"] = "retention-result.v1"
    command_id: str
    case_id: str
    action: Literal["CLOSE_CASE", "DELETE_CASE", "TTL_EXPIRED"]
    deleted_resource_ids: list[str] = Field(default_factory=list)
    preserved_resource_ids: list[str] = Field(default_factory=list)
    audit_tombstone_id: str


class CaseWindow(ContractModel):
    start: datetime
    end: datetime

    @field_validator("start", "end", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _ordered(self) -> CaseWindow:
        if self.end < self.start:
            raise ValueError("case window end must not precede start")
        return self


class LastKnown(ContractModel):
    camera_id: str | None = None
    zone_id: str | None = None
    time: datetime

    @field_validator("time", mode="after")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _one_location(self) -> LastKnown:
        if bool(self.camera_id) == bool(self.zone_id):
            raise ValueError("last_known requires exactly one camera_id or zone_id")
        return self


class CreateCaseBody(ContractModel):
    case_id: str
    operator_id: str
    target_id: str
    reference_image_ref: ArtifactRef
    last_known: LastKnown
    window: CaseWindow
    profile: Literal["ADULT", "CHILD", "ELDERLY"] = "ADULT"
    site_id: str
    site_version: str


class ReviewDecisionBody(ContractModel):
    decision_id: str
    candidate_id: str
    tracklet_id: str
    decision: Literal["CONFIRM", "REJECT"]
    operator_id: str
    occurred_at: datetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware_occurred(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class CaseCommandBody(ContractModel):
    command_id: str
    action: Literal["CLOSE", "EXPORT", "DELETE"]
    operator_id: str
    occurred_at: datetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _aware_occurred(cls, value: datetime) -> datetime:
        return aware_utc(value)  # type: ignore[return-value]


class FakeHttpRequest(ContractModel):
    request_id: str
    method: Literal["GET", "POST"]
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = Field(
        default=None,
        validation_alias="json",
        serialization_alias="json",
    )

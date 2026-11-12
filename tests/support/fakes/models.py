"""Models used only to drive deterministic test doubles."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field, field_validator, model_validator

from javes_contracts import ArtifactRef, ContractModel


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


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
        return aware_utc(value)

    @model_validator(mode="after")
    def _ordered(self) -> FakeObservation:
        if self.t_end < self.t_start:
            raise ValueError("observation t_end must not precede t_start")
        return self

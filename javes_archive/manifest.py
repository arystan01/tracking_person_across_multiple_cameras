"""Immutable metadata contract for the checked-in NVIDIA 12-camera archive."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ArchiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoverageWindow(ArchiveModel):
    start_offset_s: float = Field(ge=0.0)
    end_offset_s: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> CoverageWindow:
        if self.end_offset_s <= self.start_offset_s:
            raise ValueError("archive coverage window must be ordered")
        return self


class ArchiveCamera(ArchiveModel):
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    container: Literal["mp4"]
    codec: str
    resolution_px: tuple[int, int]
    fps: float = Field(gt=0.0)
    frame_count: int = Field(gt=0)
    duration_s: float = Field(gt=0.0)
    timeline_offset_s: float = 0.0
    coverage: list[CoverageWindow]

    @field_validator("relative_path", mode="after")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("archive relative_path must be a safe POSIX path")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError("archive relative_path must be a safe POSIX path")
        return path.as_posix()

    @model_validator(mode="after")
    def _valid_metadata(self) -> ArchiveCamera:
        width, height = self.resolution_px
        if width <= 0 or height <= 0:
            raise ValueError("archive resolution must be positive")
        if not self.coverage:
            raise ValueError("archive camera needs at least one coverage window")
        ordered = sorted(self.coverage, key=lambda item: item.start_offset_s)
        if ordered != self.coverage:
            raise ValueError("archive coverage windows must be sorted")
        if any(
            right.start_offset_s < left.end_offset_s
            for left, right in zip(ordered, ordered[1:])
        ):
            raise ValueError("archive coverage windows cannot overlap")
        if ordered[-1].end_offset_s > self.duration_s + 1e-6:
            raise ValueError("archive coverage exceeds video duration")
        return self


class NvidiaArchiveManifest(ArchiveModel):
    manifest_version: Literal["nvidia-archive-manifest.v1"]
    dataset: Literal["NVIDIA-MTMC-12cam"]
    version: str
    timeline_origin: datetime
    source_clock: Literal["UTC_CORRECTED"] = "UTC_CORRECTED"
    cameras: dict[str, ArchiveCamera]

    @field_validator("timeline_origin", mode="after")
    @classmethod
    def _aware_origin(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("archive timeline_origin must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _complete_12_camera_fixture(self) -> NvidiaArchiveManifest:
        expected = {f"Cam{index:03d}" for index in range(1, 13)}
        if set(self.cameras) != expected:
            missing = sorted(expected - set(self.cameras))
            extra = sorted(set(self.cameras) - expected)
            raise ValueError(
                f"NVIDIA archive must contain Cam001..Cam012; "
                f"missing={missing}, extra={extra}"
            )
        if not self.version or self.version.lower() == "latest":
            raise ValueError("archive version must be immutable and cannot be latest")
        return self

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from javes_contracts.boundary import aware_utc


class CameraSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start: datetime
    archive_id: str | None = None

    @field_validator("start", mode="after")
    @classmethod
    def _aware_start(cls, value: datetime) -> datetime:
        return aware_utc(value)


class PerceptionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: dict[str, CameraSource]
    detector_weights: str = "models/yolo26n.pt"
    tracker: str = "bytetrack.yaml"
    reid_model: str = "osnet_ain_x1_0"
    reid_weights: str = "models/osnet_ain_x1_0_msmt17.pt"
    model_version: str = "osnet_ain_x1_0_msmt17"
    device: str = "auto"
    sample_fps: float = 5.0
    det_conf: float = 0.1
    imgsz: int = 640
    min_track_frames: int = 3
    embed_every: int = 2
    min_crop_height: int = 48
    crop_filter: bool = True
    min_embed_conf: float = 0.4
    edge_margin_px: int = 2
    max_overlap: float = 0.3
    calib_a: float = 26.0
    calib_b: float = -15.49
    score_floor: float = 0.1
    calibration_version: str = "osnet-ain-market1501-logit-v1"


def load_settings(path: str | Path) -> PerceptionSettings:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = PerceptionSettings.model_validate(data)
    base = path.parent
    for camera in settings.cameras.values():
        if not Path(camera.path).is_absolute():
            camera.path = str((base / camera.path).resolve())
    for field in ("detector_weights", "reid_weights"):
        value = getattr(settings, field)
        if not Path(value).is_absolute() and (base / value).exists():
            setattr(settings, field, str((base / value).resolve()))
    return settings

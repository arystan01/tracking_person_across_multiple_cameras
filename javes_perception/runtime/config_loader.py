"""Filesystem-backed Perception configuration loader."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..core.config import PerceptionSettings


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

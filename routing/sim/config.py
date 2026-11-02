"""Parameters for synthetic data generation, separate from RoutingParams."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SimParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = 42
    p_det: float = Field(default=0.85, ge=0.0, le=1.0)
    appearance_mean: float = Field(default=0.8, ge=0.0, le=1.0)
    appearance_noise_std: float = Field(default=0.1, ge=0.0)
    false_positive_rate: float = Field(default=0.02, ge=0.0, le=1.0)
    camera_dwell_min_s: float = Field(default=5.0, gt=0.0)
    camera_dwell_max_s: float = Field(default=30.0, gt=0.0)
    true_edge_time_scale_min: float = Field(default=0.6, gt=0.0)
    true_edge_time_scale_max: float = Field(default=1.4, gt=0.0)
    clock_skew_s: float = Field(default=3.0, ge=0.0)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SimParams":
        with Path(path).open(encoding="utf-8") as stream:
            return cls.model_validate(yaml.safe_load(stream) or {})

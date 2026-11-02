from __future__ import annotations
import yaml
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

class RoutingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    B: int = Field(default=5, ge=1)
    K: int = Field(default=4, ge=1)
    p_det: float = Field(default=0.85, ge=0.0, le=1.0)
    merge_gap_s: float = Field(default=10.0, ge=0.0)
    max_gap_s: float = Field(default=2400.0, gt=0.0)
    amb_delta: float = Field(default=1.0, ge=0.0)
    max_retries: int = Field(default=2, ge=0)
    new_hyp_floor: float = -20.0
    camera_minute_budget: float = Field(default=500.0, gt=0.0)
    max_speed: float = 2.0  # Deprecated alias
    gate_speed: float = 4.0
    walk_speed: float = 1.3
    tmax_over_ttyp: float = 2.5
    clock_tolerance_s: float = Field(default=3.0, ge=0.0)
    use_st: bool = True
    amb_priority_bonus: float = 2.0
    dormant_watch_boost: float = 1.2
    score_clip_min: float = Field(default=0.01, gt=0.0, lt=0.5)
    rejected_top_n: int = 5
    tail_factor: float = Field(default=3.0, gt=1.0)
    tail_eps: float = Field(default=0.05, gt=0.0, lt=1.0)
    heartbeat_s: float = Field(default=5.0, gt=0.0)
    cands_per_probe: int = 3
    merge_dist_frac: float = 0.25
    leak_prior: float = 0.02
    unknown_dwell: dict[str, float] = Field(default_factory=lambda: {"median_s": 600.0, "sigma": 1.2})
    group_corr: float = 0.7
    min_coverage: float = 0.5
    quiet_window_s: float = 120.0
    crowd_penalty_k: float = 0.3
    confirm_after_n: int = 2
    manual_time_sigma_s: float = 120.0
    profiles: dict[str, dict[str, float]] = Field(default_factory=lambda: {
        "ADULT": {"tmin_k": 1.0, "ttyp_k": 1.0, "tmax_k": 1.0},
        "CHILD": {"tmin_k": 0.6, "ttyp_k": 1.0, "tmax_k": 1.6},
        "ELDERLY": {"tmin_k": 1.0, "ttyp_k": 1.5, "tmax_k": 2.0},
    })
    live_current_priority: float = 2.0
    blind_path_priority: float = 0.3
    max_hidden_edges: int = Field(default=5, ge=2)
    dormant_border_priority: float = 0.5
    estimate_neighbour_share: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_profiles(self) -> RoutingParams:
        for name in ("ADULT", "CHILD", "ELDERLY"):
            factors = self.profiles.get(name)
            if factors is None or any(float(factors.get(key, 0.0)) <= 0 for key in ("tmin_k", "ttyp_k", "tmax_k")):
                raise ValueError(f"invalid timing profile {name}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> RoutingParams:
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
        if not data:
            data = {}
        return cls(**data)

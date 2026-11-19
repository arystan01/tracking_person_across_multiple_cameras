from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

class RoutingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1"
    B: int = Field(default=5, ge=1)
    K: int = Field(default=4, ge=1)
    p_det: float = Field(default=0.85, ge=0.0, le=1.0)
    merge_gap_s: float = Field(default=10.0, ge=0.0)
    observed_gap_s: float = Field(default=2.0, ge=0.0)
    gap_bridge_max_s: float = Field(default=10.0, gt=0.0)
    max_gap_s: float = Field(default=2400.0, gt=0.0)
    historical_shell_s: float = Field(default=300.0, gt=0.0)
    amb_delta: float = Field(default=1.0, ge=0.0)
    max_retries: int = Field(default=2, ge=0)
    new_hyp_floor: float = -20.0
    camera_minute_budget: float = Field(default=500.0, gt=0.0)
    max_speed: float = 2.0
    max_calibration_error_m: float = Field(default=2.0, ge=0.0)
    overlap_time_tolerance_s: float = Field(default=1.0, ge=0.0)
    overlap_position_tolerance_m: float = Field(default=0.75, gt=0.0)
    anchor_probe_window_s: float = Field(default=5.0, gt=0.0)
    max_walk_snap_m: float = Field(default=3.0, gt=0.0)
    walk_alternative_limit: int = Field(default=3, ge=0)
    reachable_time_step_s: float = Field(default=0.5, gt=0.0)
    reachable_heading_kappa: float = Field(default=1.0, ge=0.0)
    reachable_probability_epsilon: float = Field(default=1e-6, gt=0.0)
    reachable_max_states: int = Field(default=10000, ge=1)
    reachable_method_version: str = "relative-timegraph-v1"
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
    edge_timing_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)
    timing_calibration_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    timing_calibration_split: str | None = None
    timing_calibration_scenario_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_profiles(self) -> RoutingParams:
        for name in ("ADULT", "CHILD", "ELDERLY"):
            factors = self.profiles.get(name)
            if factors is None or any(float(factors.get(key, 0.0)) <= 0 for key in ("tmin_k", "ttyp_k", "tmax_k")):
                raise ValueError(f"invalid timing profile {name}")
        for edge_id, timing in self.edge_timing_overrides.items():
            try:
                t_min = float(timing["t_min"])
                t_typ = float(timing["t_typ"])
                t_max = float(timing["t_max"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid timing override for edge {edge_id!r}") from exc
            if not (0 < t_min <= t_typ <= t_max):
                raise ValueError(f"invalid timing override order for edge {edge_id!r}")
        if self.timing_calibration_split not in {None, "development"}:
            raise ValueError("Routing timing calibration may use development split only")
        self.timing_calibration_scenario_ids = sorted(
            set(self.timing_calibration_scenario_ids)
        )
        return self

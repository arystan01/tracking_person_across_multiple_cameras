from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


class ContractModel(BaseModel):
    """Strict model that emits the canonical PDF field names."""

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )


# Shared contracts (3, 6, 7). Additions remain optional.
class Tracklet(ContractModel):
    id: str = Field(
        validation_alias=AliasChoices("id", "tracklet_id"),
        serialization_alias="tracklet_id",
    )
    case_id: str | None = None
    probe_id: str | None = None
    camera: str = Field(
        validation_alias=AliasChoices("camera", "camera_id"),
        serialization_alias="camera_id",
    )
    t_in: datetime = Field(
        validation_alias=AliasChoices("t_in", "t_start"),
        serialization_alias="t_start",
    )
    t_out: datetime = Field(
        validation_alias=AliasChoices("t_out", "t_end"),
        serialization_alias="t_end",
    )
    bbox_first: tuple[float, float, float, float]
    bbox_last: tuple[float, float, float, float]
    open: bool = False
    clipped_in: bool = Field(
        default=False,
        validation_alias=AliasChoices("clipped_in", "clipped_start"),
        serialization_alias="clipped_start",
    )
    clipped_out: bool = Field(
        default=False,
        validation_alias=AliasChoices("clipped_out", "clipped_end"),
        serialization_alias="clipped_end",
    )
    crop_paths: list[str] = Field(default_factory=list)
    embedding_path: str | None = None
    clip_ref: dict[str, Any] | str | None = None

    @field_validator("t_in", "t_out", mode="after")
    @classmethod
    def _aware_times(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _ordered_times(self) -> Tracklet:
        if self.t_out < self.t_in:
            raise ValueError("tracklet t_end must not be before t_start")
        return self


class Candidate(ContractModel):
    id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "candidate_id"),
        serialization_alias="candidate_id",
    )
    tracklet_id: str
    score: float = Field(
        validation_alias=AliasChoices("score", "appearance_score"),
        serialization_alias="appearance_score",
        ge=0.0,
        le=1.0,
    )
    cosine: float | None = None
    calibration_version: str | None = None
    association_id: str | None = None
    # Legacy convenience only. Tracklet is authoritative on the wire.
    camera: str | None = Field(default=None, exclude=True)
    t_in: datetime | None = Field(default=None, exclude=True)
    t_out: datetime | None = Field(default=None, exclude=True)

    @field_validator("t_in", "t_out", mode="after")
    @classmethod
    def _aware_optional_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class ProbeStatus(str, Enum):
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


class ProbeRequest(ContractModel):
    probe_id: str
    camera: str = Field(
        validation_alias=AliasChoices("camera", "camera_id"),
        serialization_alias="camera_id",
    )
    t_from: datetime
    t_to: datetime | None
    priority: float = 1.0
    reason: str = "ROUTE_EXPANSION"
    hypothesis_ids: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    retry_of: str | None = None

    @field_validator("t_to", mode="before")
    @classmethod
    def _parse_open(cls, value: Any) -> Any:
        return None if isinstance(value, str) and value.upper() == "OPEN" else value

    @field_validator("t_from", "t_to", mode="after")
    @classmethod
    def _aware_probe_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _ordered_window(self) -> ProbeRequest:
        if self.t_to is not None and self.t_to < self.t_from:
            raise ValueError("probe t_to must not be before t_from")
        self.hypothesis_ids = sorted(set(self.hypothesis_ids))
        return self

    @field_serializer("t_to")
    def _serialize_open(self, value: datetime | None) -> datetime | str:
        return "OPEN" if value is None else value


class ProbeResult(ContractModel):
    probe_id: str
    status: ProbeStatus
    tracklets: list[Tracklet] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    coverage_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    processed_duration_s: float | None = Field(default=None, ge=0.0)
    n_tracklets_total: int | None = Field(default=None, ge=0)
    stream_closed: bool = False
    error_code: str | None = None


# Site graph (contract 8).
class EdgeDef(ContractModel):
    id: str
    source: str = Field(
        validation_alias=AliasChoices("source", "from"),
        serialization_alias="from",
    )
    target: str = Field(
        validation_alias=AliasChoices("target", "to"),
        serialization_alias="to",
    )
    via: str | None = None
    t_min: float = Field(gt=0.0)
    t_typ: float = Field(gt=0.0)
    t_max: float = Field(gt=0.0)
    d_m: float | None = Field(default=None, gt=0.0)
    floor: str | None = None
    kind: str | None = None
    vertical: bool = False
    map_path: list[tuple[float, float]] = Field(default_factory=list)


class RegionDef(ContractModel):
    name: str
    edges: list[str] = Field(default_factory=list)
    polygon: list[tuple[float, float]]


class CameraDef(ContractModel):
    id: str
    zone: str = ""
    floor: str | None = None
    map_xy: tuple[float, float] | None = None
    clock_offset_s: float = 0.0
    regions: list[RegionDef] = Field(default_factory=list)
    p_det: float | None = Field(default=None, ge=0.0, le=1.0)
    busy: bool = False
    appearance_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    available_from: datetime | None = None

    @field_validator("available_from", mode="after")
    @classmethod
    def _aware_available_from(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class DwellDef(ContractModel):
    median_s: float = Field(default=30.0, gt=0.0)
    sigma: float = Field(default=0.8, gt=0.0)


class BlindNodeDef(ContractModel):
    id: str
    kind: Literal["room", "elevator", "escalator", "blind"] = "blind"
    borders: list[str] = Field(default_factory=list)
    exits: dict[str, float] = Field(default_factory=dict)
    floor: str | None = None
    map_xy: tuple[float, float] | None = None
    dwell_median_s: float = Field(default=30.0, gt=0.0, exclude=True)
    dwell_sigma: float = Field(default=0.8, gt=0.0, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _flatten_dwell(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("dwell"), dict):
            data = dict(data)
            dwell = data.pop("dwell")
            data.setdefault("dwell_median_s", dwell.get("median_s", 30.0))
            data.setdefault("dwell_sigma", dwell.get("sigma", 0.8))
        return data

    @model_serializer(mode="wrap")
    def _serialize_blind(self, handler):
        data = handler(self)
        data["dwell"] = {"median_s": self.dwell_median_s, "sigma": self.dwell_sigma}
        return data


class OutsideDef(ContractModel):
    entrances: dict[str, float]
    dwell_median_s: float = Field(default=300.0, gt=0.0, exclude=True)
    dwell_sigma: float = Field(default=1.0, gt=0.0, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _flatten_dwell(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("dwell"), dict):
            data = dict(data)
            dwell = data.pop("dwell")
            data.setdefault("dwell_median_s", dwell.get("median_s", 300.0))
            data.setdefault("dwell_sigma", dwell.get("sigma", 1.0))
        return data

    @model_serializer(mode="wrap")
    def _serialize_outside(self, handler):
        data = handler(self)
        data["dwell"] = {"median_s": self.dwell_median_s, "sigma": self.dwell_sigma}
        return data


class MapFloor(ContractModel):
    id: str
    image: str | None = None
    size_px: tuple[int, int]
    m_per_px: float | None = Field(default=None, gt=0.0)


class MapDef(ContractModel):
    floors: list[MapFloor] = Field(default_factory=list)


class SiteGraph(ContractModel):
    site_id: str = Field(
        default="site",
        validation_alias=AliasChoices("site_id", "site"),
        serialization_alias="site",
    )
    version: str = "1"
    timezone: str = "UTC"
    cameras: dict[str, CameraDef]
    edges: list[EdgeDef]
    blind_nodes: list[BlindNodeDef] = Field(default_factory=list)
    outside: OutsideDef | None = None
    map: MapDef | None = None
    max_gap_s: float = Field(default=2400.0, gt=0.0)

    def get_edges_from(self, node: str) -> list[EdgeDef]:
        return [edge for edge in self.edges if edge.source == node]

    def get_edges_to(self, node: str) -> list[EdgeDef]:
        return [edge for edge in self.edges if edge.target == node]

    def get_node_ids(self) -> list[str]:
        nodes = list(self.cameras) + [node.id for node in self.blind_nodes]
        if self.outside is not None:
            nodes.append("OUTSIDE")
        return nodes

    def node_floor_xy(self, node: str) -> tuple[str | None, tuple[float, float] | None]:
        if node in self.cameras:
            camera = self.cameras[node]
            return camera.floor, camera.map_xy
        blind = next((item for item in self.blind_nodes if item.id == node), None)
        if blind is not None:
            return blind.floor, blind.map_xy
        return None, None


# Routing state and contract 9.
class Visit(ContractModel):
    id: str = Field(validation_alias=AliasChoices("id", "visit_id"), serialization_alias="visit_id")
    camera: str = Field(validation_alias=AliasChoices("camera", "camera_id"), serialization_alias="camera_id")
    zone: str | None = None
    t_in: datetime
    t_out: datetime
    tracklet_ids: list[str] = Field(default_factory=list)
    entry_edges: list[str] = Field(default_factory=list)
    exit_edges: list[str] = Field(default_factory=list)
    score: float = Field(validation_alias=AliasChoices("score", "appearance_score"), serialization_alias="appearance_score")
    open: bool = False
    clipped_in: bool = False
    clipped_out: bool = False
    long_gap: bool = False
    source: Literal["PERCEPTION", "LAST_KNOWN", "MANUAL"] = "PERCEPTION"
    flags: list[str] = Field(default_factory=list)

    @field_validator("t_in", "t_out", mode="after")
    @classmethod
    def _aware_visit_times(cls, value: datetime) -> datetime:
        return _utc(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _normalise_visit(self) -> Visit:
        if self.t_out < self.t_in:
            raise ValueError("visit t_out must not be before t_in")
        self.tracklet_ids = sorted(set(self.tracklet_ids))
        self.entry_edges = sorted(set(self.entry_edges))
        self.exit_edges = sorted(set(self.exit_edges))
        flags = set(self.flags)
        if self.long_gap:
            flags.add("LONG_GAP")
        self.flags = sorted(flags)
        return self


class Transition(ContractModel):
    from_visit_id: str
    to_visit_id: str
    edge_ids: list[str] = Field(default_factory=list)
    alternative_edge_paths: list[list[str]] = Field(default_factory=list)
    via_nodes: list[str] = Field(default_factory=list)
    kind: Literal["EDGE", "BLIND", "OUTSIDE", "UNKNOWN", "VERTICAL", "GAP"] = "EDGE"
    flags: list[str] = Field(default_factory=list)


class HypothesisStatus(str, Enum):
    ALIVE = "ALIVE"
    DORMANT = "DORMANT"
    ENDED = "ENDED"


class Hypothesis(ContractModel):
    id: str
    visits: list[Visit]
    transitions: list[Transition] = Field(default_factory=list)
    current_node: str
    status: HypothesisStatus
    score: float = 0.0
    via_alternatives: list[list[str]] = Field(default_factory=list)
    miss_count: int = 0
    requires_confirmation: bool = False
    reject_reason: str | None = None
    gate_margin_s: float | None = None


class SearchMode(str, Enum):
    CATCHING_UP = "CATCHING_UP"
    LIVE_TRACKING = "LIVE_TRACKING"
    WAITING = "WAITING"
    PAST_SUMMARY = "PAST_SUMMARY"


class ZoneEstimate(ContractModel):
    zone: str
    probability: float = Field(ge=0.0, le=1.0)


class Estimate(ContractModel):
    zones: list[ZoneEstimate]
    probability_kind: Literal["RELATIVE"] = "RELATIVE"
    valid_until: datetime | None = None
    last_observed_camera: str | None = None
    last_observed_time: datetime | None = None
    unobserved_windows: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("valid_until", "last_observed_time", mode="after")
    @classmethod
    def _aware_estimate_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class Ambiguity(ContractModel):
    hypothesis_ids: list[str]
    diverge_cameras: list[str] = Field(default_factory=list)
    diverge_edge_ids: list[str] = Field(default_factory=list)
    score_delta: float


class RejectedCandidate(ContractModel):
    tracklet_id: str | None = None
    camera_id: str | None = None
    appearance_score: float | None = None
    reject_reason: str
    gate_margin_s: float | None = None
    hypothesis_id: str | None = None


class StopReason(str, Enum):
    ALL_ENDED = "ALL_ENDED"
    BUDGET_SPENT = "BUDGET_SPENT"
    MAX_GAP = "MAX_GAP"
    OPERATOR_CLOSED = "OPERATOR_CLOSED"


class Anchors(ContractModel):
    last_known_camera: str | None = Field(default=None, validation_alias=AliasChoices("last_known_camera", "camera_id"))
    last_known_zone: str | None = Field(default=None, validation_alias=AliasChoices("last_known_zone", "zone_id"))
    last_known_time: datetime | None = None
    confirm: list[str] = Field(default_factory=list, validation_alias=AliasChoices("confirm", "confirm_tracklet_ids"))
    reject: list[str] = Field(default_factory=list, validation_alias=AliasChoices("reject", "reject_tracklet_ids"))
    operator_closed: bool = False

    @model_validator(mode="before")
    @classmethod
    def _flatten_last_known(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("last_known"), dict):
            data = dict(data)
            last_known = data.pop("last_known")
            data.setdefault("last_known_camera", last_known.get("camera", last_known.get("camera_id")))
            data.setdefault("last_known_zone", last_known.get("zone", last_known.get("zone_id")))
            data.setdefault("last_known_time", last_known.get("time"))
        return data

    @field_validator("last_known_time", mode="after")
    @classmethod
    def _aware_anchor_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value)


class CaseInfo(ContractModel):
    case_id: str
    target_id: str
    start_time: datetime
    description: str = ""
    profile: Literal["ADULT", "CHILD", "ELDERLY"] = "ADULT"
    window_start: datetime | None = None
    window_end: datetime | None = None
    last_known_camera: str | None = None
    last_known_zone: str | None = None
    last_known_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_case_request(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        window = data.pop("window", None)
        if isinstance(window, dict):
            data.setdefault("window_start", window.get("start"))
            data.setdefault("window_end", window.get("end"))
        last_known = data.pop("last_known", None)
        if isinstance(last_known, dict):
            data.setdefault("last_known_camera", last_known.get("camera", last_known.get("camera_id")))
            data.setdefault("last_known_zone", last_known.get("zone", last_known.get("zone_id")))
            data.setdefault("last_known_time", last_known.get("time"))
        if data.get("start_time") is None:
            data["start_time"] = data.get("last_known_time") or data.get("window_start")
        return data

    @field_validator("start_time", "window_start", "window_end", "last_known_time", mode="after")
    @classmethod
    def _aware_case_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _case_defaults(self) -> CaseInfo:
        if self.window_start is None:
            self.window_start = self.last_known_time or self.start_time
        if self.window_end is not None and self.window_end < self.window_start:
            raise ValueError("case window_end must not be before window_start")
        return self


class ProbedWindow(ContractModel):
    camera: str = Field(validation_alias=AliasChoices("camera", "camera_id"), serialization_alias="camera_id")
    t_from: datetime
    t_to: datetime | None
    probe_id: str
    status: ProbeStatus
    coverage_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    found: bool = False

    @field_validator("t_to", mode="before")
    @classmethod
    def _parse_open(cls, value: Any) -> Any:
        return None if isinstance(value, str) and value.upper() == "OPEN" else value

    @field_validator("t_from", "t_to", mode="after")
    @classmethod
    def _aware_window_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_serializer("t_to")
    def _serialize_open(self, value: datetime | None) -> datetime | str:
        return "OPEN" if value is None else value


class SearchState(ContractModel):
    case_id: str
    profile: str = "ADULT"
    round_number: int = 0
    state_version: int = 0
    site_version: str = "1"
    params_version: str = "1"
    calibration_version: str | None = None
    id_counter: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    rejected_tracklet_ids: list[str] = Field(default_factory=list)
    confirmed_tracklet_ids: list[str] = Field(default_factory=list)
    processed_result_ids: list[str] = Field(default_factory=list)
    processed_duration_s_by_probe: dict[str, float] = Field(default_factory=dict)
    probed_windows: list[ProbedWindow] = Field(default_factory=list)
    pending_probes: list[ProbeRequest] = Field(default_factory=list)
    pending_review_visits: list[Visit] = Field(default_factory=list)
    unobserved_windows: list[dict[str, Any]] = Field(default_factory=list)
    mode: SearchMode = SearchMode.CATCHING_UP
    camera_minutes_used: float = 0.0
    baseline_started: bool = False
    done: bool = False
    stop_reason: StopReason | None = None

    @field_validator("window_start", "window_end", mode="after")
    @classmethod
    def _aware_state_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @model_validator(mode="after")
    def _sort_state_ids(self) -> SearchState:
        self.rejected_tracklet_ids = sorted(set(self.rejected_tracklet_ids))
        self.confirmed_tracklet_ids = sorted(set(self.confirmed_tracklet_ids))
        self.processed_result_ids = sorted(set(self.processed_result_ids))
        return self


class StepResult(ContractModel):
    state: SearchState
    probes: list[ProbeRequest]
    hypotheses: list[Hypothesis]
    best: Hypothesis | str | None = None
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    estimate: Estimate | None = None
    mode: SearchMode
    done: bool = False
    stop_reason: StopReason | None = None

    @field_serializer("best")
    def _serialize_best(self, value: Hypothesis | str | None) -> str | None:
        return value.id if isinstance(value, Hypothesis) else value


class RenderedFloorMap(ContractModel):
    floor_id: str
    mime_type: Literal["image/svg+xml"] = "image/svg+xml"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    svg: str
    alt_text: str


class RenderedRouteBundle(ContractModel):
    route_revision: int
    best_hypothesis_id: str | None = None
    maps: list[RenderedFloorMap] = Field(default_factory=list)


class RoutingWorkerResult(ContractModel):
    step_result: StepResult
    rendered_route: RenderedRouteBundle


StepResult.model_rebuild()

"""Canonical internal models. External formats belong in adapters.py."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

CAMERAS = {
    "A": {"name": "West entrance", "x": 110, "y": 200},
    "B": {"name": "Central atrium", "x": 330, "y": 200},
    "C": {"name": "Food court", "x": 550, "y": 100},
    "D": {"name": "East exit", "x": 550, "y": 310},
}
# Minimum travel time in seconds. Undirected edges for this sample site.
EDGES = [("A", "B", 45), ("B", "C", 60), ("B", "D", 90)]

def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid ISO 8601 timestamp: {value}") from exc
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone, e.g. +09:00 or Z.")
    return result

@dataclass
class Candidate:
    id: str
    camera_id: str
    observed_at: str
    appearance_score: float
    decision: str = "UNREVIEWED"
    clip_state: str = "HOLD"
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Case:
    id: str
    title: str
    operator: str
    camera_id: str
    last_known_at: str
    start: str
    end: str
    created_at: str
    status: str = "SEARCHING"
    source: str = "demo"
    round: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    reference_b64: str | None = None
    expires_at: str | None = None
    notes: str = ""
    saved: bool = False
    timeout_seconds: int = 300

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["candidates"] = [Candidate(**item) for item in data["candidates"]]
        return cls(**data)

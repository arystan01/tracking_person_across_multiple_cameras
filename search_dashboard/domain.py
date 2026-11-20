"""Normalized control-API view models. No routing calculations."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

CAMERAS = {
    "A": {"name": "West entrance", "x": 110, "y": 200},
    "B": {"name": "Central atrium", "x": 330, "y": 200},
    "C": {"name": "Food court", "x": 550, "y": 100},
    "D": {"name": "East exit", "x": 550, "y": 310},
}
# Display-only site links, with no speed or travel-time parameters.
SITE_LINKS = [("A", "B"), ("B", "C"), ("B", "D")]

def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid ISO timestamp: {value}") from exc
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
    return result

@dataclass
class Candidate:
    id: str
    camera_id: str
    observed_at: str
    appearance_score: float
    decision: str = "UNREVIEWED"
    clip_state: str = "HOLD"
    observation_type: str = "NORMAL"
    review_state: str = "READY"
    requested_decision: str | None = None
    review_revision: int | None = None
    explanation: str = ""
    clip_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class CaseView:
    id: str
    title: str
    operator: str
    last_known: dict
    window: dict
    created_at: str
    lifecycle: str = "PREPARING"
    mode: str = "CATCHING_UP"
    done: bool = False
    stop_reason: str | None = None
    revision: int = 1
    route_revision: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    route: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    unobserved_windows: list[dict] = field(default_factory=list)
    reference_b64: str | None = None
    notes: str = ""
    source: str = "demo"
    merged_clip_url: str | None = None
    # Local demo-worker persistence only; never part of exported CaseView.
    simulation: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        if "lifecycle" not in data:
            # Preserve old cases as historical snapshots; do not rerun or expire them.
            candidates = []
            for row in data.get("candidates", []):
                row = dict(row)
                if row.get("decision") == "RECALCULATE":
                    row["decision"] = "UNREVIEWED"
                    row["explanation"] = "Legacy recalculation request; retained for reference."
                candidates.append(Candidate(**row))
            return cls(id=data["id"], title=data["title"], operator=data["operator"],
                last_known={"camera_id": data["camera_id"], "time": data["last_known_at"]},
                window={"start": data["start"], "end": data["end"]}, created_at=data["created_at"],
                lifecycle="CLOSED", mode="PAST_SUMMARY", done=True, source="legacy",
                candidates=candidates, reference_b64=data.get("reference_b64"), notes=data.get("notes", ""),
                errors=[{"code": "LEGACY_SNAPSHOT", "message": "Previous prototype case. Original decisions are preserved; no routing snapshot was stored."}])
        data["candidates"] = [Candidate(**item) for item in data.get("candidates", [])]
        return cls(**data)

Case = CaseView  # Existing SQLite serialization boundary.

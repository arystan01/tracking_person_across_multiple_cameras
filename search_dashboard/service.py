"""UI-independent case lifecycle, route calculation, and retention rules."""
import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from .domain import CAMERAS, EDGES, Case, timestamp

ACTIVE = {"SEARCHING", "IN_COVERAGE", "OUT_OF_COVERAGE"}

def travel_seconds(source, target):
    distances = {key: float("inf") for key in CAMERAS}
    distances[source] = 0
    for _ in CAMERAS:
        for a, b, seconds in EDGES:
            distances[b] = min(distances[b], distances[a] + seconds)
            distances[a] = min(distances[a], distances[b] + seconds)
    return distances[target]

def ranked_candidates(case):
    anchor = timestamp(case.last_known_at)
    rows = []
    for c in case.candidates:
        gap = abs((timestamp(c.observed_at) - anchor).total_seconds())
        minimum = travel_seconds(case.camera_id, c.camera_id)
        feasible = gap >= minimum
        rows.append({"candidate": c, "feasible": feasible,
                     "rank_score": round(c.appearance_score * (1 if feasible else 0.25), 3),
                     "reason": f"{gap:.0f}s available / {minimum:.0f}s minimum travel"})
    return sorted(rows, key=lambda row: row["rank_score"], reverse=True)

def route(case):
    # The declared seed and operator-confirmed sightings are explicitly distinguished.
    points = [{"camera_id": case.camera_id, "observed_at": case.last_known_at, "source": "REPORTED", "candidate_id": None}]
    points += [{"camera_id": c.camera_id, "observed_at": c.observed_at,
                "source": "CONFIRMED", "candidate_id": c.id}
               for c in case.candidates if c.decision == "CONFIRMED"]
    points.sort(key=lambda p: timestamp(p["observed_at"]))
    for i, point in enumerate(points):
        previous = points[i-1] if i else None
        point["travel_conflict"] = bool(previous and
            (timestamp(point["observed_at"]) - timestamp(previous["observed_at"])).total_seconds()
            < travel_seconds(previous["camera_id"], point["camera_id"]))
    return points

class CaseManager:
    def __init__(self, store):
        self.store = store

    def create(self, title, operator, camera, last_known, start, end, reference=None, notes="", now=None):
        if not title.strip() or not operator.strip():
            raise ValueError("Case title and operator ID are required.")
        if camera not in CAMERAS:
            raise ValueError("Unknown last-known camera.")
        if not timestamp(start) <= timestamp(last_known) <= timestamp(end) or timestamp(start) == timestamp(end):
            raise ValueError("The time window must have positive duration and contain the last-known time.")
        if reference and len(reference) > 5_000_000:
            raise ValueError("Reference image must be smaller than 5 MB.")
        case = Case(uuid4().hex[:12], title.strip(), operator.strip(), camera, last_known,
                    start, end, (now or datetime.now(timezone.utc)).isoformat(),
                    reference_b64=base64.b64encode(reference).decode() if reference else None, notes=notes)
        self.store.save(case, operator, "CREATED")
        return case

    def search(self, case_id, provider, operator):
        case = self.store.get(case_id)
        if case.saved or case.status not in ACTIVE:
            raise ValueError("Closed or expired cases cannot be searched.")
        if case.round:
            raise ValueError("This prototype accepts one batch per case; create a new case to search another batch.")
        candidates = provider.search(case)  # Validate the entire batch before any write.
        case.candidates, case.source = candidates, provider.name
        case.round += 1
        self.store.save(case, operator, "SEARCH_COMPLETED", f"{len(candidates)} candidates; provider={provider.name}")
        return case

    def review(self, case_id, candidate_id, decision, operator):
        case = self.store.get(case_id)
        if case.saved or case.status not in ACTIVE:
            raise ValueError("Review is locked after case closure.")
        if decision not in {"CONFIRMED", "REJECTED", "UNREVIEWED", "RECALCULATE"}:
            raise ValueError("Unknown review decision.")
        candidate = next((c for c in case.candidates if c.id == candidate_id), None)
        if candidate is None:
            raise ValueError("Unknown candidate.")
        candidate.decision = decision
        candidate.clip_state = {"CONFIRMED": "CONFIRMED", "REJECTED": "RELEASED", "UNREVIEWED": "HOLD", "RECALCULATE": "HOLD"}[decision]
        if case.status == "IN_COVERAGE" and not any(c.decision == "CONFIRMED" for c in case.candidates):
            case.status = "SEARCHING"
        self.store.save(case, operator, "REVIEWED", f"{candidate_id}: {decision}; status={case.status}")

    def transition(self, case_id, status, operator, retention_hours=24, now=None):
        case = self.store.get(case_id)
        if case.saved:
            raise ValueError("Saved cases are view-only.")
        allowed = {"SEARCHING": {"IN_COVERAGE", "OUT_OF_COVERAGE", "CLOSED"},
                   "IN_COVERAGE": {"OUT_OF_COVERAGE", "CLOSED"},
                   "OUT_OF_COVERAGE": {"IN_COVERAGE", "CLOSED"}}
        if status not in allowed.get(case.status, set()):
            raise ValueError(f"Cannot transition {case.status} to {status}.")
        if status == "IN_COVERAGE" and not any(c.decision == "CONFIRMED" for c in case.candidates):
            raise ValueError("Confirm a candidate before marking the target in coverage.")
        case.status = status
        if status == "CLOSED":
            if not 1 <= retention_hours <= 720:
                raise ValueError("Retention must be between 1 and 720 hours.")
            case.reference_b64 = None
            case.notes = ""
            case.candidates = [c for c in case.candidates if c.decision == "CONFIRMED"]
            for candidate in case.candidates:
                candidate.metadata = {}
            case.expires_at = ((now or datetime.now(timezone.utc)) + timedelta(hours=retention_hours)).isoformat()
        self.store.save(case, operator, status)

    def save_result(self, case_id, operator):
        case = self.store.get(case_id)
        if case.saved or not case.round:
            raise ValueError("Run a search before saving an unsaved case.")
        if any(c.decision in {"UNREVIEWED", "RECALCULATE"} for c in case.candidates):
            raise ValueError("Review all candidates and resolve recalculation requests first.")
        case.saved = True
        self.store.save(case, operator, "RESULT_SAVED")

    def recalculate(self, case_id, operator):
        case = self.store.get(case_id)
        if case.saved or case.status not in ACTIVE:
            raise ValueError("This case is view-only.")
        if case.source != "demo":
            raise ValueError("Recalculation for imported results needs the backend.")
        for candidate in case.candidates:
            if candidate.decision == "RECALCULATE":
                candidate.decision = "UNREVIEWED"
                candidate.metadata["recalculation"] = "Demo retry: original observation returned for review"
        self.store.save(case, operator, "DEMO_RECALCULATED")

    def cleanup(self, now=None):
        now = now or datetime.now(timezone.utc)
        count = 0
        for case in self.store.list():
            if case.status == "CLOSED" and case.expires_at and timestamp(case.expires_at) <= now:
                # Retain only a minimal receipt; remove title, seed location, and payload.
                self.store.delete(case.id, "retention-worker")
                count += 1
        return count

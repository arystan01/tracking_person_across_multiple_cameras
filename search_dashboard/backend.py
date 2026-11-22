"""Control API boundary and LOCAL SIMULATOR. Replace DemoControlAPI with a real client.

Only this simulator advances states or synthesizes routing output. The dashboard
never chooses recording windows, ranks candidates, or reconstructs a route.
Demo work advances on polling, not in a real background routing worker.
"""
import base64
from functools import wraps
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4
from .domain import CAMERAS, Candidate, CaseView, timestamp
from .service import can_review

class ControlAPI(Protocol):
    def create_case(self, title, operator, camera_id, last_known_time, reference=None, notes="") -> CaseView: ...
    def get_case_view(self, case_id) -> CaseView: ...
    def submit_decision(self, case_id, candidate_id, decision, operator) -> CaseView: ...
    def close_case(self, case_id, operator) -> CaseView: ...
    def delete_case(self, case_id, operator) -> None: ...

def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        # Serialize local demo-worker mutations across Streamlit sessions.
        with self.store.lock:
            return method(self, *args, **kwargs)
    return call

class DemoControlAPI:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @serialized
    def create_case(self, title, operator, camera_id, last_known_time, reference=None, notes=""):
        now = self.clock()
        if not title.strip() or not operator.strip():
            raise ValueError("Case title and operator are required.")
        if camera_id not in CAMERAS:
            raise ValueError("Select a known camera.")
        if timestamp(last_known_time) > now:
            raise ValueError("Last-known time cannot be in the future.")
        if reference and len(reference) > 5_000_000:
            raise ValueError("Reference image must be smaller than 5 MB.")
        case = CaseView(id=uuid4().hex[:12], title=title.strip(), operator=operator,
            last_known={"camera_id": camera_id, "time": last_known_time},
            window={"start": last_known_time, "end": now.isoformat()}, created_at=now.isoformat(),
            reference_b64=base64.b64encode(reference).decode() if reference else None, notes=notes)
        case.route = [{"camera_id": camera_id, "observed_at": last_known_time, "source": "REPORTED"}]
        self.store.save(case, operator, "CREATED")
        return case

    @serialized
    def get_case_view(self, case_id):
        case = self.store.get(case_id)
        if case.source != "demo" or case.lifecycle in {"CLOSED", "FAILED"}:
            return case
        now = self.clock()
        elapsed = (now - timestamp(case.created_at)).total_seconds()
        changed = False
        # Process accepted decisions even when no new sightings arrive.
        for candidate in case.candidates:
            if candidate.review_state == "PENDING" and timestamp(candidate.metadata["apply_at"]) <= now:
                candidate.decision = "CONFIRMED" if candidate.requested_decision == "CONFIRM" else "REJECTED"
                candidate.clip_state = "CONFIRMED" if candidate.decision == "CONFIRMED" else "RELEASED"
                candidate.review_state = "APPLIED"
                case.route_revision += 1
                candidate.review_revision = case.route_revision
                # A stand-in for routing's recomputed route, never UI computation.
                case.route = [{"camera_id": case.last_known["camera_id"], "observed_at": case.last_known["time"], "source": "REPORTED"}]
                case.route += [{"camera_id": c.camera_id, "observed_at": c.observed_at, "source": "CONFIRMED", "candidate_id": c.id}
                               for c in case.candidates if c.decision == "CONFIRMED"]
                case.route.sort(key=lambda point: timestamp(point["observed_at"]))
                changed = True
        target = ("PREPARING", "CATCHING_UP") if elapsed < 2 else ("SEARCHING", "CATCHING_UP" if elapsed < 30 else "LIVE_TRACKING" if elapsed < 60 else "WAITING")
        if (case.lifecycle, case.mode) != target:
            case.lifecycle, case.mode = target
            changed = True
        # Fictional observations within the server-selected historical window.
        cameras = [case.last_known["camera_id"]] + [c for c in CAMERAS if c != case.last_known["camera_id"]]
        start, end = timestamp(case.window["start"]), timestamp(case.window["end"])
        for i, available in enumerate([2, 8, 14, 40]):
            cid = f"demo-{cameras[i]}"
            if elapsed >= available and not any(c.id == cid for c in case.candidates):
                seen = start + (end - start) * (i / 3) if i < 3 else end + timedelta(seconds=35)
                case.candidates.append(Candidate(cid, cameras[i], seen.isoformat(), [0.96, 0.89, 0.84, 0.72][i],
                    observation_type="LONG_GAP" if i == 2 else "NORMAL",
                    explanation="Long gap between sightings. Confirm the match to add this sighting to the main route." if i == 2 else ""))
                changed = True
        if elapsed >= 45 and not case.errors:
            case.errors = [{"code": "CAMERA_UNAVAILABLE", "camera_id": cameras[-1], "message": "Demo camera interruption. Other search work continues."}]
            case.unobserved_windows = [{"camera_id": cameras[-1], "start": (end + timedelta(seconds=40)).isoformat(),
                                       "end": (end + timedelta(seconds=45)).isoformat(), "reason": "Camera unavailable"}]
            changed = True
        if changed:
            case.revision += 1
            self.store.save(case, "demo-control", "VIEW_UPDATED", f"revision={case.revision}; route_revision={case.route_revision}")
        return case

    @serialized
    def submit_decision(self, case_id, candidate_id, decision, operator):
        if decision not in {"CONFIRM", "REJECT"}:
            raise ValueError("Only CONFIRM and REJECT are supported.")
        case = self.get_case_view(case_id)
        if not can_review(case):
            raise ValueError("This case is read-only.")
        candidate = next((c for c in case.candidates if c.id == candidate_id), None)
        if candidate is None:
            raise ValueError("Candidate not found.")
        if candidate.review_state == "PENDING" or candidate.decision != "UNREVIEWED":
            existing = candidate.requested_decision or {"CONFIRMED": "CONFIRM", "REJECTED": "REJECT"}.get(candidate.decision)
            if decision != existing:
                raise ValueError("Conflicting decision rejected. Decisions cannot be changed.")
            return case  # Idempotent retry of the same decision.
        candidate.review_state = "PENDING"
        candidate.requested_decision = decision
        candidate.metadata.update(apply_at=(self.clock()+timedelta(seconds=3)).isoformat(),
                                  base_revision=case.revision, base_route_revision=case.route_revision)
        case.revision += 1
        self.store.save(case, operator, "DECISION_ACCEPTED", f"{candidate_id}: {decision}; routing update pending")
        return case

    @serialized
    def close_case(self, case_id, operator):
        case = self.get_case_view(case_id)
        if case.done or case.lifecycle == "FAILED":
            return case
        if any(c.review_state == "PENDING" for c in case.candidates):
            raise ValueError("Wait for pending decisions to finish before closing the demo case.")
        case.lifecycle, case.mode, case.done, case.stop_reason = "CLOSED", "PAST_SUMMARY", True, "OPERATOR_CLOSED"
        case.revision += 1
        self.store.save(case, operator, "OPERATOR_CLOSED")
        return case

    @serialized
    def delete_case(self, case_id, operator):
        case = self.get_case_view(case_id)
        if not case.done and case.lifecycle != "FAILED":
            raise ValueError("Close the search before deleting the case.")
        self.store.delete(case_id, operator)

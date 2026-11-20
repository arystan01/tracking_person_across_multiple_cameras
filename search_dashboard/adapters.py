"""Translate a future control API at this boundary, without changing the UI.

CaseView is a local normalized schema, not an assertion about REST endpoint names.
"""
import json
from .domain import CaseView, timestamp

class CaseViewAdapter:
    def decode(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("CaseView must be an object.")
        if "lifecycle" not in payload:
            raise ValueError("Control CaseView requires lifecycle.")
        case = CaseView.from_dict(payload)
        if case.lifecycle not in {"PREPARING", "SEARCHING", "CLOSED", "FAILED"}:
            raise ValueError("Unknown lifecycle.")
        if case.mode not in {"CATCHING_UP", "LIVE_TRACKING", "WAITING", "PAST_SUMMARY"}:
            raise ValueError("Unknown view mode.")
        if type(case.done) is not bool:
            raise ValueError("done must be a boolean.")
        if any(type(v) is not int or v < 0 for v in (case.revision, case.route_revision)):
            raise ValueError("Revisions must be nonnegative integers.")
        if case.done and (case.lifecycle, case.mode) != ("CLOSED", "PAST_SUMMARY"):
            raise ValueError("Completed cases require CLOSED and PAST_SUMMARY.")
        if case.stop_reason not in {None, "ALL_ENDED", "MAX_GAP", "BUDGET_SPENT", "OPERATOR_CLOSED"}:
            raise ValueError("Unknown stop reason.")
        timestamp(case.created_at)
        timestamp(case.last_known["time"])
        timestamp(case.window["start"])
        timestamp(case.window["end"])
        return case

class JsonExport:
    def encode(self, case, route=None):
        payload = case.to_dict()
        for field in ("reference_b64", "simulation"):
            payload.pop(field, None)
        for candidate in payload["candidates"]:
            candidate.pop("metadata", None)
        return json.dumps({"schema_version": "2.0", "case_view": payload}, indent=2).encode()

"""Translate changing wire formats into stable domain models.

A perception service only needs to implement SearchProvider.search(case).
No external payload is consumed directly by the UI or case manager.
"""
import csv
import io
import json
import math
from datetime import timedelta
from typing import Protocol
from .domain import Candidate, Case, CAMERAS, timestamp

class SearchProvider(Protocol):
    name: str
    def search(self, case: Case) -> list[Candidate]: ...

class DemoProvider:
    name = "demo"
    def search(self, case):
        anchor = timestamp(case.last_known_at)
        # Relative to the chosen seed camera, using real site connectivity.
        sequences = {"A": ["A", "B", "C", "D"], "B": ["B", "A", "C", "D"],
                     "C": ["C", "B", "A", "D"], "D": ["D", "B", "A", "C"]}
        result = []
        for i, camera in enumerate(sequences[case.camera_id]):
            seen = anchor + timedelta(seconds=i * 180)
            if timestamp(case.start) <= seen <= timestamp(case.end):
                result.append(Candidate(f"demo-{camera}", camera, seen.isoformat(),
                                        [0.96, 0.89, 0.84, 0.72][i], metadata={"simulated": True}))
        # Deliberately implausible high-appearance match to demonstrate filtering.
        seen = anchor + timedelta(seconds=5)
        if seen <= timestamp(case.end):
            result.append(Candidate("demo-distractor", sequences[case.camera_id][-1],
                                    seen.isoformat(), 0.98, metadata={"simulated": True}))
        return result

DEFAULT_MAPPING = {key: key for key in ("id", "camera_id", "observed_at", "appearance_score")}

class ImportProvider:
    name = "import"
    def __init__(self, payload: bytes, format: str, mapping: dict | None = None):
        self.payload, self.format = payload, format
        self.mapping = mapping or DEFAULT_MAPPING

    def search(self, case):
        if len(self.payload) > 5_000_000:
            raise ValueError("Candidate files must be smaller than 5 MB.")
        try:
            raw = self.payload.decode("utf-8-sig")
            if self.format == "json":
                data = json.loads(raw)
                if isinstance(data, dict):
                    if data.get("schema_version", "1.0") != "1.0":
                        raise ValueError("Unsupported schema_version; expected 1.0.")
                    data = data.get("candidates")
            elif self.format == "csv":
                data = list(csv.DictReader(io.StringIO(raw)))
            else:
                raise ValueError("Supported import formats: json, csv.")
        except (UnicodeError, json.JSONDecodeError, csv.Error) as exc:
            raise ValueError(f"Cannot read candidate file: {exc}") from exc
        if not isinstance(data, list) or len(data) > 1000:
            raise ValueError("Expected a list with at most 1,000 candidates.")
        if set(self.mapping) != set(DEFAULT_MAPPING) or any(not isinstance(v, str) or not v for v in self.mapping.values()):
            raise ValueError("Mapping must provide the four canonical fields with nonempty source column names.")
        candidates, ids = [], set()
        for index, row in enumerate(data, 1):
            try:
                if not isinstance(row, dict):
                    raise ValueError("Expected an object")
                values = {field: row[source] for field, source in self.mapping.items()}
                identifier = str(values["id"]).strip()
                camera = str(values["camera_id"])
                score = float(values["appearance_score"])
                seen = timestamp(values["observed_at"])
                if not identifier or identifier in ids:
                    raise ValueError("Candidate IDs must be nonempty and unique")
                if camera not in CAMERAS:
                    raise ValueError(f"Unknown camera {camera}; expected A, B, C, or D")
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("appearance_score must be finite and between 0 and 1")
                if not timestamp(case.start) <= seen <= timestamp(case.end):
                    raise ValueError("Observation lies outside this case's search window")
                ids.add(identifier)
                metadata = {k: v for k, v in row.items() if k not in self.mapping.values()}
                candidates.append(Candidate(identifier, camera, seen.isoformat(), score, metadata=metadata))
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise ValueError(f"Row {index}: {exc}") from exc
        return candidates

class ExportAdapter(Protocol):
    def encode(self, case: Case, route: list[dict]) -> bytes: ...

class JsonExport:
    def encode(self, case, route):
        payload = case.to_dict()
        payload.pop("reference_b64", None)
        # Export reviewed evidence metadata, never arbitrary provider metadata.
        for candidate in payload["candidates"]:
            candidate.pop("metadata", None)
        return json.dumps({"schema_version": "1.0", "case": payload, "route": route}, indent=2).encode()

class CsvExport:
    def encode(self, case, route):
        output = io.StringIO()
        fields = ["id", "camera_id", "observed_at", "appearance_score", "decision", "clip_state"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for candidate in case.candidates:
            row = vars(candidate)
            writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                             for k, v in row.items() if k in fields})
        return output.getvalue().encode()

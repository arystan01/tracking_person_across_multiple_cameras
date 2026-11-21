"""Bounded, deterministic, redacted security-audit export."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

from javes_contracts import AuditExportManifest, AuditExportRequest
from javes_main.domain import AuditEvent
from javes_main.ports import ArtifactStore, CaseRepository, Clock, IdGenerator


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|credential|api[_-]?key|lease|embedding|reference_image|object_key|artifact_ref|stream_ref|path)",
    re.IGNORECASE,
)


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class AuditExportService:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        artifacts: ArtifactStore,
        clock: Clock,
        ids: IdGenerator,
        register_artifact,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.clock = clock
        self.ids = ids
        self.register_artifact = register_artifact

    @staticmethod
    def _keys(export_id: str) -> tuple[str, str]:
        namespace = hashlib.sha256(export_id.encode("utf-8")).hexdigest()[:32]
        root = f"audit/exports/{namespace}"
        return f"{root}/events.ndjson", f"{root}/manifest.json"

    @staticmethod
    def _filters(request: AuditExportRequest) -> dict[str, Any]:
        return {
            "from_time": request.from_time.isoformat() if request.from_time else None,
            "to_time": request.to_time.isoformat() if request.to_time else None,
            "case_ids": sorted(set(request.case_ids)),
            "actor_ids": sorted(set(request.actor_ids)),
            "event_types": sorted(set(request.event_types)),
        }

    def _event_chunks(
        self, request: AuditExportRequest, state: dict[str, Any]
    ) -> Iterator[bytes]:
        after = None
        while True:
            page = self.repository.list_audit_events(
                after=after,
                from_time=request.from_time,
                to_time=request.to_time,
                case_ids=set(request.case_ids) or None,
                actor_ids=set(request.actor_ids) or None,
                event_types=set(request.event_types) or None,
                limit=request.page_size,
            )
            if not page:
                return
            for event in page:
                envelope = {
                    "schema_version": "javes-audit-event.v1",
                    "source": "IAM" if event.case_id == "__iam__" else "CASE",
                    "event": {
                        "event_id": event.event_id,
                        "case_id": event.case_id,
                        "event_type": event.event_type,
                        "actor_id": event.actor_id,
                        "occurred_at": event.occurred_at.isoformat(),
                        "payload": _redact(event.payload),
                    },
                }
                if state["first_event_id"] is None:
                    state["first_event_id"] = event.event_id
                state["last_event_id"] = event.event_id
                state["row_count"] += 1
                yield (
                    json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            last = page[-1]
            after = (last.occurred_at, last.event_id)
            if len(page) < request.page_size:
                return

    def create(
        self, request: AuditExportRequest, *, actor_id: str
    ) -> AuditExportManifest:
        started_at = self.clock.now()
        effective = request.model_copy(
            update={"to_time": request.to_time or started_at}, deep=True
        )
        events_key, manifest_key = self._keys(request.export_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"request": request.model_dump(mode="json"), "actor_id": actor_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = f"audit-export:{request.export_id}"
        with self.repository.transaction():
            self.repository.lock_idempotency(idempotency_key)
            previous = self.repository.get_idempotency(idempotency_key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError(
                        "audit export_id was reused with different filters"
                    )
                data, _ = self.artifacts.get(manifest_key)
                return AuditExportManifest.model_validate_json(data)
            state = {"row_count": 0, "first_event_id": None, "last_event_id": None}
            _, size, digest = self.artifacts.put_stream(
                events_key,
                self._event_chunks(effective, state),
                "application/x-ndjson",
            )
            manifest = AuditExportManifest(
                export_id=request.export_id,
                created_at=started_at,
                created_by=actor_id,
                filters=self._filters(effective),
                row_count=state["row_count"],
                first_event_id=state["first_event_id"],
                last_event_id=state["last_event_id"],
                size_bytes=size,
                sha256=digest,
                events_url=f"/v1/audit/exports/{request.export_id}/events",
                manifest_url=f"/v1/audit/exports/{request.export_id}/manifest",
            )
            manifest_data = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
            self.artifacts.put(manifest_key, manifest_data, "application/json")
            self.register_artifact(
                request.export_id, events_key, "AUDIT_NDJSON", size, digest
            )
            self.register_artifact(
                request.export_id,
                manifest_key,
                "AUDIT_MANIFEST",
                len(manifest_data),
                hashlib.sha256(manifest_data).hexdigest(),
            )
            self.repository.append_event(
                AuditEvent(
                    event_id=self.ids.new(
                        "audit_evt", "AUDIT_EXPORTED", request.export_id
                    ),
                    case_id="__iam__",
                    event_type="AUDIT_EXPORTED",
                    actor_id=actor_id,
                    occurred_at=started_at,
                    payload={
                        "export_id": request.export_id,
                        "row_count": manifest.row_count,
                        "sha256": manifest.sha256,
                    },
                )
            )
            self.repository.remember_idempotency(
                idempotency_key,
                fingerprint,
                {"export_id": request.export_id, "sha256": manifest.sha256},
            )
            return manifest

    def read(self, export_id: str, kind: str) -> tuple[bytes, str]:
        events_key, manifest_key = self._keys(export_id)
        return self.artifacts.get(events_key if kind == "events" else manifest_key)

    def iter_read(self, export_id: str, kind: str):
        events_key, manifest_key = self._keys(export_id)
        return self.artifacts.iter_chunks(
            events_key if kind == "events" else manifest_key
        )

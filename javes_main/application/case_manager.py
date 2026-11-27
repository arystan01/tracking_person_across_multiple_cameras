"""Framework-independent Controls and Case Manager application service."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from javes_contracts import (
    Anchors,
    ArtifactManifest,
    ArtifactRef,
    CaseCommandBody,
    CaseInfo,
    Candidate,
    CaseDetailView,
    CreateLegalHoldBody,
    ClipLifecycleState,
    ClipPurgeCommand,
    ClipPurgeResult,
    ClipProcessingStatus,
    ClipRecord,
    ClipStateCommand,
    CreateCaseBody,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
    Hypothesis,
    LiveFeedUpdate,
    LegalHoldView,
    PerceptionProbeJob,
    ProbeResult,
    ProbeStatus,
    RetentionClass,
    RetentionCommand,
    RetentionHoldCommand,
    RetentionHoldResult,
    RetentionResource,
    RetentionResult,
    RevokeDecisionBody,
    ReleaseLegalHoldBody,
    DecisionRecordView,
    ReviewDecisionBody,
    RoutingWorkerResult,
    SearchMode,
    TargetGalleryCommand,
    TargetGalleryResult,
    Tracklet,
)

from javes_main.domain.models import (
    AuditEvent,
    CaseDataKind,
    CaseDataRecord,
    CaseLifecycle,
    CaseRecord,
    CaseSummary,
    CaseView,
    JobRecord,
    JobState,
    LegalHoldRecord,
    OutboxRecord,
    OutboxTopic,
    ProbeResultRecord,
    RoutingTriggerRecord,
    ReferenceImageUploadStatus,
    StoredRoute,
)
from javes_main.ports import (
    ArtifactStore,
    CaseRepository,
    ClipPort,
    Clock,
    IdGenerator,
    LiveFeedPort,
    PerceptionPort,
    RetentionPort,
    RoutingPort,
    SiteConfigPort,
)
from javes_main.projections import build_case_view
from javes_main.persistence import RevisionConflict
from javes_main.application.errors import ReferenceUploadError, ReplayEvidenceIncomplete
from javes_contracts.acceptance import (
    check_clip_state_result,
    check_cut_result,
    check_export_result,
)


logger = logging.getLogger(__name__)

# Product policy: case content is retained for fourteen calendar days after the
# case is closed. Internally all timestamps are UTC, so one day is 24 hours.
DEFAULT_RETENTION_DAYS = 14
DEFAULT_RETENTION_TTL_S = DEFAULT_RETENTION_DAYS * 24 * 60 * 60


def _fingerprint(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CaseManager:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        artifacts: ArtifactStore,
        sites: SiteConfigPort,
        routing: RoutingPort,
        perception: PerceptionPort,
        clip: ClipPort,
        retention: RetentionPort,
        live_feed: LiveFeedPort,
        clock: Clock,
        ids: IdGenerator,
        clip_padding_s: float = 2.0,
        job_deadline_s: float = 120.0,
        retention_ttl_s: float = DEFAULT_RETENTION_TTL_S,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.sites = sites
        self.routing = routing
        self.perception = perception
        self.clip = clip
        self.retention = retention
        self.live_feed = live_feed
        self.clock = clock
        self.ids = ids
        self.clip_padding_s = clip_padding_s
        self.job_deadline_s = job_deadline_s
        if retention_ttl_s <= 0:
            raise ValueError("retention_ttl_s must be positive")
        self.retention_ttl_s = retention_ttl_s

    def _event(
        self,
        case_id: str,
        event_type: str,
        *,
        actor_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = self.clock.now()
        event = AuditEvent(
            event_id=self.ids.new(
                "evt", case_id, event_type, now.isoformat(), _fingerprint(payload or {})
            ),
            case_id=case_id,
            event_type=event_type,
            actor_id=actor_id,
            occurred_at=now,
            payload=payload or {},
        )
        self.repository.append_event(event)

    def _idempotent_response(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        previous = self.repository.get_idempotency(key)
        if previous is None:
            return None
        if previous[0] != fingerprint:
            raise ValueError("idempotency key reused with different payload")
        return previous[1]

    def cleanup_expired_reference_uploads(self, *, limit: int = 100) -> int:
        from .reference_uploads import ReferenceImageUploadService

        return ReferenceImageUploadService(
            repository=self.repository,
            artifacts=self.artifacts,
            clock=self.clock,
            ids=self.ids,
        ).cleanup_expired(limit=limit)

    def _decision_history(self, case_id: str) -> list[DecisionRecordView]:
        decisions = {
            item.record_id: ReviewDecisionBody.model_validate(item.payload)
            for item in self.repository.list_case_data(
                case_id, kinds={CaseDataKind.OPERATOR_DECISION.value}
            )
        }
        revocations = {
            str(item.payload["decision_id"]): RevokeDecisionBody.model_validate(
                item.payload
            )
            for item in self.repository.list_case_data(
                case_id,
                kinds={CaseDataKind.OPERATOR_DECISION_REVOCATION.value},
            )
        }
        route_by_decision: dict[str, int] = {}
        for item in self.repository.list_case_data(
            case_id, kinds={CaseDataKind.ROUTING_INPUT.value, CaseDataKind.ROUTING_RUN.value}
        ):
            decision_id = item.payload.get("source_decision_id")
            route_revision = item.payload.get("output_route_revision", item.payload.get("route_revision"))
            if decision_id is not None and route_revision is not None:
                route_by_decision[str(decision_id)] = max(
                    route_by_decision.get(str(decision_id), 0), int(route_revision)
                )
        values: list[DecisionRecordView] = []
        for decision_id, decision in decisions.items():
            revoked = revocations.get(decision_id)
            values.append(
                DecisionRecordView(
                    **decision.model_dump(mode="python"),
                    active=revoked is None,
                    revocation_id=(revoked.revocation_id if revoked else None),
                    revoked_by=(revoked.operator_id if revoked else None),
                    revoked_at=(revoked.occurred_at if revoked else None),
                    revocation_reason=(revoked.reason if revoked else None),
                    resulting_route_revision=route_by_decision.get(decision_id),
                )
            )
        return sorted(values, key=lambda item: (item.occurred_at, item.decision_id))

    def list_decisions(self, case_id: str) -> list[DecisionRecordView]:
        self.repository.get(case_id)
        return self._decision_history(case_id)

    def _active_decisions(self, case_id: str) -> list[DecisionRecordView]:
        return [item for item in self._decision_history(case_id) if item.active]

    @staticmethod
    def _legal_hold_view(record: LegalHoldRecord) -> LegalHoldView:
        return LegalHoldView.model_validate(record.model_dump(mode="python"))

    def list_legal_holds(self, case_id: str) -> list[LegalHoldView]:
        case = self.repository.get(case_id)
        return [
            self._legal_hold_view(item)
            for item in sorted(
                case.legal_holds.values(),
                key=lambda value: (value.set_at, value.hold_id),
            )
        ]

    def set_legal_hold(
        self,
        case_id: str,
        body: CreateLegalHoldBody,
        *,
        idempotency_key: str,
    ) -> LegalHoldView:
        storage_key = f"case:{case_id}:legal-hold:set:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return next(
                item
                for item in self.list_legal_holds(case_id)
                if item.hold_id == body.hold_id
            )
        case = self.repository.get(case_id)
        if case.lifecycle == CaseLifecycle.DELETED:
            self._event(
                case_id,
                "LEGAL_HOLD_SET_DENIED",
                actor_id=body.operator_id,
                payload={"hold_id": body.hold_id, "reason": "case_deleted"},
            )
            raise ValueError("cannot set legal hold on a deleted case")
        if case.lifecycle == CaseLifecycle.DELETE_REQUESTED:
            self._event(
                case_id,
                "LEGAL_HOLD_SET_DENIED",
                actor_id=body.operator_id,
                payload={"hold_id": body.hold_id, "reason": "deletion_requested"},
            )
            raise ValueError("cannot set legal hold after deletion was requested")
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                return next(
                    item
                    for item in self.list_legal_holds(case_id)
                    if item.hold_id == body.hold_id
                )
            case = self.repository.get(case_id)
            if body.expected_case_revision != case.revision:
                raise ValueError(
                    "expected_case_revision does not match current case revision"
                )
            if body.hold_id in case.legal_holds:
                raise ValueError("hold_id already exists")
            expected = case.revision
            record = LegalHoldRecord(
                hold_id=body.hold_id,
                case_id=case_id,
                external_ticket_id=body.external_ticket_id,
                reason=body.reason,
                set_by=body.operator_id,
                set_at=body.occurred_at,
            )
            case.legal_holds[body.hold_id] = record
            case.updated_at = self.clock.now()
            self.repository.save(case, expected)
            command = RetentionHoldCommand(
                command_id=self.ids.new("retention_hold", case_id, "set", body.hold_id),
                case_id=case_id,
                hold_id=body.hold_id,
                action="SET",
                external_ticket_id=body.external_ticket_id,
                reason=body.reason,
                actor_id=body.operator_id,
                occurred_at=body.occurred_at,
            )
            self._enqueue(
                case_id,
                OutboxTopic.RETENTION_HOLD,
                f"retention-hold:{command.command_id}",
                {"command": command.model_dump(mode="json")},
            )
            self._record_case_data(
                case_id,
                CaseDataKind.LEGAL_HOLD,
                body.hold_id,
                body.model_dump(mode="json"),
                schema_version="legal-hold.v1alpha1",
                occurred_at=body.occurred_at,
            )
            self._event(
                case_id,
                "LEGAL_HOLD_SET",
                actor_id=body.operator_id,
                payload={
                    "hold_id": body.hold_id,
                    "external_ticket_id": body.external_ticket_id,
                },
            )
            self.repository.remember_idempotency(
                storage_key, fingerprint, {"case_id": case_id, "hold_id": body.hold_id}
            )
        return self._legal_hold_view(record)

    def release_legal_hold(
        self,
        case_id: str,
        body: ReleaseLegalHoldBody,
        *,
        idempotency_key: str,
    ) -> LegalHoldView:
        storage_key = f"case:{case_id}:legal-hold:release:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return next(
                item
                for item in self.list_legal_holds(case_id)
                if item.hold_id == body.hold_id
            )
        case = self.repository.get(case_id)
        if case.lifecycle == CaseLifecycle.DELETED:
            self._event(
                case_id,
                "LEGAL_HOLD_RELEASE_DENIED",
                actor_id=body.operator_id,
                payload={"hold_id": body.hold_id, "reason": "case_deleted"},
            )
            raise ValueError("cannot release legal hold on a deleted case")
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                return next(
                    item
                    for item in self.list_legal_holds(case_id)
                    if item.hold_id == body.hold_id
                )
            case = self.repository.get(case_id)
            if body.expected_case_revision != case.revision:
                raise ValueError(
                    "expected_case_revision does not match current case revision"
                )
            record = case.legal_holds.get(body.hold_id)
            if record is None:
                raise KeyError(f"unknown legal hold {body.hold_id!r}")
            if not record.active:
                raise ValueError("legal hold is already released")
            if record.release_pending:
                raise ValueError("legal hold release is already pending")
            expected = case.revision
            record.release_pending = True
            record.release_id = body.release_id
            record.release_reason = body.reason
            record.released_by = body.operator_id
            record.released_at = body.occurred_at
            case.updated_at = self.clock.now()
            self.repository.save(case, expected)
            command = RetentionHoldCommand(
                command_id=body.release_id,
                case_id=case_id,
                hold_id=body.hold_id,
                action="RELEASE",
                external_ticket_id=body.external_ticket_id,
                reason=body.reason,
                actor_id=body.operator_id,
                occurred_at=body.occurred_at,
            )
            self._enqueue(
                case_id,
                OutboxTopic.RETENTION_HOLD,
                f"retention-hold:{command.command_id}",
                {"command": command.model_dump(mode="json")},
            )
            self._record_case_data(
                case_id,
                CaseDataKind.LEGAL_HOLD_RELEASE,
                body.release_id,
                body.model_dump(mode="json"),
                schema_version="legal-hold-release.v1alpha1",
                occurred_at=body.occurred_at,
            )
            self._event(
                case_id,
                "LEGAL_HOLD_RELEASE_REQUESTED",
                actor_id=body.operator_id,
                payload={
                    "hold_id": body.hold_id,
                    "release_id": body.release_id,
                    "external_ticket_id": body.external_ticket_id,
                },
            )
            self.repository.remember_idempotency(
                storage_key, fingerprint, {"case_id": case_id, "hold_id": body.hold_id}
            )
        return self._legal_hold_view(record)

    def _enqueue(
        self,
        case_id: str,
        topic: OutboxTopic,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> None:
        now = self.clock.now()
        scoped_dedupe_key = f"{case_id}:{dedupe_key}"
        self.repository.add_outbox(
            OutboxRecord(
                message_id=self.ids.new("outbox", topic.value, scoped_dedupe_key),
                case_id=case_id,
                topic=topic.value,
                dedupe_key=scoped_dedupe_key,
                payload=payload,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    def _record_case_data(
        self,
        case_id: str,
        kind: CaseDataKind,
        record_id: str,
        payload: dict[str, Any],
        *,
        schema_version: str = "case-data.v1",
        occurred_at: datetime | None = None,
    ) -> None:
        self.repository.add_case_data(
            CaseDataRecord(
                record_id=record_id,
                case_id=case_id,
                kind=kind,
                schema_version=schema_version,
                occurred_at=occurred_at or self.clock.now(),
                payload=payload,
            )
        )

    def _prepare_route_artifacts(self, case_id: str, revision: int, maps) -> list[dict]:
        """Reserve ownership before CAS; a rollback must not orphan or poison SVG.

        Bytes are published only after CAS, under their immutable content hash.
        Intent/Retention survives a later aggregate rollback or process crash.
        Only the committed ROUTE_REVISION maps make a blob publicly readable.
        """
        prepared: list[dict] = []
        with self.repository.transaction():
            if self.repository.get(case_id).lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
                raise RevisionConflict("case no longer accepts route artifacts")
            existing = {
                item.record_id: item.payload
                for item in self.repository.list_case_data(
                    case_id, kinds={CaseDataKind.ARTIFACT_INTENT.value}
                )
            }
            for item in maps:
                data = item.svg.encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                key = f"cases/{case_id}/routes/{revision}/{digest}/{item.floor_id}.svg"
                artifact_id = self.ids.new("artifact", key)
                artifact = existing.get(artifact_id)
                if artifact is None:
                    manifest = ArtifactManifest(
                        object_key=key, kind="route_svg", size_bytes=len(data),
                        sha256=digest, created_at=self.clock.now(),
                        retention_class="case_14d", mime_type=item.mime_type,
                    )
                    artifact = {
                        "artifact_id": artifact_id,
                        "manifest": manifest.model_dump(mode="json"),
                        "route_revision": revision, "floor_id": item.floor_id,
                        "width": item.width, "height": item.height, "alt_text": item.alt_text,
                    }
                    self._record_case_data(
                        case_id, CaseDataKind.ARTIFACT_INTENT, artifact_id, artifact
                    )
                self._enqueue_retention_resource(RetentionResource(
                    resource_id=self.ids.new("resource", case_id, key),
                    case_id=case_id, kind="ROUTE_SVG",
                    retention_class=RetentionClass.CASE_RESULT,
                    artifact_ref=ArtifactRef(object_key=key, sha256=digest),
                ))
                prepared.append(artifact)
        return prepared

    def _stored_route_key(self, case_id: str, revision: int, floor_id: str) -> str:
        for record in self.repository.list_case_data(
            case_id, kinds={CaseDataKind.ROUTE_REVISION.value}
        ):
            if record.payload.get("revision") != revision:
                continue
            for item in record.payload.get("maps", []):
                if item.get("floor_id") == floor_id:
                    # Previously committed revisions retain their original key.
                    return item.get("object_key") or f"cases/{case_id}/routes/{revision}/{floor_id}.svg"
        raise KeyError("route revision or floor is not committed")

    def _superseded_route_resources(self, case: CaseRecord) -> list[RetentionResource]:
        previous = case.latest_route
        if previous is None:
            return []
        resources: list[RetentionResource] = []
        for item in previous.rendered.maps:
            key = self._stored_route_key(case.request.case_id, previous.revision, item.floor_id)
            legacy = f"cases/{case.request.case_id}/routes/{previous.revision}/{item.floor_id}.svg"
            resource_id = (
                self.ids.new("resource", case.request.case_id, "route", previous.revision, item.floor_id)
                if key == legacy else self.ids.new("resource", case.request.case_id, key)
            )
            resources.append(RetentionResource(
                resource_id=resource_id, case_id=case.request.case_id,
                kind="SUPERSEDED_ROUTE_SVG", retention_class=RetentionClass.EPHEMERAL,
                artifact_ref=ArtifactRef(
                    object_key=key,
                    sha256=None if key == legacy else hashlib.sha256(item.svg.encode("utf-8")).hexdigest(),
                ), revision=2,
            ))
        return resources

    @staticmethod
    def _artifact_ref_from_path(value: str) -> ArtifactRef:
        """Convert Perception-owned paths into stable Retention locators."""

        if urlparse(value).scheme:
            return ArtifactRef(uri=value)
        if value.startswith("/"):
            return ArtifactRef(uri=f"file://{value}")
        # Component workers share the private ArtifactStore mount.  Relative
        # locators are therefore canonical object keys, not component-specific
        # URI schemes that Retention cannot resolve.
        return ArtifactRef(object_key=value)

    def register_worker_artifacts(
        self,
        case_id: str,
        task_id: str,
        manifests: list[ArtifactManifest],
    ) -> None:
        """Verify and register worker-owned files for policy deletion."""

        self.repository.get(case_id)
        prefix = f"cases/{case_id}/"
        with self.repository.transaction():
            for manifest in manifests:
                if not manifest.object_key.startswith(prefix):
                    raise ValueError("worker artifact object_key is outside its case")
                data, mime_type = self.artifacts.get(manifest.object_key)
                if len(data) != manifest.size_bytes:
                    raise ValueError("worker artifact size does not match manifest")
                if hashlib.sha256(data).hexdigest() != manifest.sha256:
                    raise ValueError("worker artifact checksum does not match manifest")
                if manifest.mime_type is not None and mime_type != manifest.mime_type:
                    raise ValueError(
                        "worker artifact MIME type does not match manifest"
                    )
                record_id = self.ids.new(
                    "artifact", case_id, task_id, manifest.object_key, manifest.sha256
                )
                self._record_case_data(
                    case_id,
                    CaseDataKind.ARTIFACT,
                    record_id,
                    manifest.model_dump(mode="json"),
                )
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource", case_id, manifest.object_key
                        ),
                        case_id=case_id,
                        kind=manifest.kind.upper(),
                        retention_class=manifest.policy_class(),
                        artifact_ref=ArtifactRef(
                            object_key=manifest.object_key,
                            sha256=manifest.sha256,
                        ),
                        revision=manifest.policy_revision,
                    )
                )

    def _enqueue_retention_resource(self, resource: RetentionResource) -> None:
        self._enqueue(
            resource.case_id,
            OutboxTopic.RETENTION_REGISTER,
            f"retention-register:{resource.resource_id}:"
            f"{resource.retention_class.value}:{_fingerprint(resource)}",
            {"resource": resource.model_dump(mode="json")},
        )

    def register_audit_artifact(
        self,
        export_id: str,
        object_key: str,
        kind: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        """Register a permanent audit artifact without exposing its filesystem path."""

        resource = RetentionResource(
            resource_id=self.ids.new("audit-resource", export_id, kind, sha256),
            case_id="__audit__",
            kind=kind,
            retention_class=RetentionClass.AUDIT,
            artifact_ref=ArtifactRef(object_key=object_key, sha256=sha256),
        )
        self._enqueue_retention_resource(resource)

    def _enqueue_routing(
        self,
        case_id: str,
        trigger_type: str,
        dedupe_key: str,
        *,
        probe_results: list[ProbeResult] | None = None,
        anchors: Anchors | None = None,
    ) -> None:
        now = self.clock.now()
        scoped_dedupe_key = f"{case_id}:{dedupe_key}"
        self.repository.add_routing_trigger(
            RoutingTriggerRecord(
                trigger_id=self.ids.new(
                    "routing_trigger", trigger_type, scoped_dedupe_key
                ),
                case_id=case_id,
                dedupe_key=scoped_dedupe_key,
                trigger_type=trigger_type,
                probe_results=probe_results or [],
                anchors=anchors or Anchors(),
                created_at=now,
                updated_at=now,
            )
        )

    def process_routing_trigger(self, trigger: RoutingTriggerRecord) -> CaseRecord:
        current_case = self.repository.get(trigger.case_id)
        if current_case.lifecycle in {
            CaseLifecycle.DELETE_REQUESTED,
            CaseLifecycle.DELETED,
            CaseLifecycle.EXPORTING,
            CaseLifecycle.SAVED,
        }:
            return current_case
        if trigger.trigger_type == "HEARTBEAT" and current_case.lifecycle != CaseLifecycle.SEARCHING:
            return current_case
        if any(
            item.record_id == trigger.trigger_id
            for item in self.repository.list_case_data(
                trigger.case_id, kinds={CaseDataKind.ROUTING_RUN.value}
            )
        ):
            return self.repository.get(trigger.case_id)
        if trigger.trigger_type == "DECISION_REVOKE":
            return self._replay_case(trigger)
        if trigger.trigger_type == "OPERATOR_DECISION":
            marker = ":decision:"
            decision_id = (
                trigger.dedupe_key.rsplit(marker, 1)[1]
                if marker in trigger.dedupe_key
                else None
            )
            if decision_id is not None and decision_id not in {
                item.decision_id for item in self._active_decisions(trigger.case_id)
            }:
                return current_case
            # A review changes the evidence set, not just the surviving beam.
            # Filtering a previously pruned beam can erase a valid prefix when
            # an unrelated later sighting is rejected, or lose a confirmed hit.
            return self._replay_case(trigger)
        return self._routing_tick(
            trigger.case_id,
            probe_results=trigger.probe_results,
            anchors=(
                trigger.anchors.model_copy(update={"review_only": True})
                if current_case.lifecycle == CaseLifecycle.CLOSED else trigger.anchors
            ),
            trigger_id=trigger.trigger_id,
            trigger_type=trigger.trigger_type,
            trigger_dedupe_key=trigger.dedupe_key,
        )

    @staticmethod
    def _case_info(case: CaseRecord) -> CaseInfo:
        body = case.request
        return CaseInfo(
            case_id=body.case_id,
            target_id=body.target_id,
            start_time=body.last_known.time,
            profile=body.profile,
            window_start=body.window.start,
            window_end=body.window.end,
            last_known_camera=body.last_known.camera_id,
            last_known_zone=body.last_known.zone_id,
            last_known_time=body.last_known.time,
            last_known_bbox_xywh=body.last_known.bbox_xywh,
            last_known_footpoint_px=body.last_known.footpoint_px,
            last_known_map_position=body.last_known.map_position,
        )

    @staticmethod
    def _initial_anchors(case: CaseRecord) -> Anchors:
        value = case.request.last_known
        return Anchors(
            last_known_camera=value.camera_id,
            last_known_zone=value.zone_id,
            last_known_time=value.time,
            last_known_bbox_xywh=value.bbox_xywh,
            last_known_footpoint_px=value.footpoint_px,
            last_known_map_position=value.map_position,
        )

    def _replay_case(self, trigger: RoutingTriggerRecord) -> CaseRecord:
        case = self.repository.get(trigger.case_id)
        if case.lifecycle not in {CaseLifecycle.SEARCHING, CaseLifecycle.CLOSED}:
            raise ValueError(
                f"cannot replay decisions while case is {case.lifecycle.value}"
            )
        if case.replay_capability != "COMPLETE":
            raise ReplayEvidenceIncomplete(
                "replay_evidence_incomplete: case has no complete immutable routing ledger"
            )
        records = self.repository.list_case_data(
            trigger.case_id, kinds={CaseDataKind.ROUTING_INPUT.value}
        )
        if not records:
            raise ReplayEvidenceIncomplete(
                "replay_evidence_incomplete: case has no immutable routing inputs"
            )
        ordered = sorted(
            records,
            key=lambda item: (int(item.payload.get("sequence", -1)), item.record_id),
        )
        expected_sequences = list(range(len(ordered)))
        actual_sequences = [int(item.payload.get("sequence", -1)) for item in ordered]
        if actual_sequences != expected_sequences:
            raise ReplayEvidenceIncomplete(
                "replay_evidence_incomplete: routing input sequence has gaps"
            )

        graph = self.sites.get_graph(case.request.site_id, case.request.site_version)
        params = self.sites.get_params(case.params_version)
        floor_assets = self.sites.get_floor_assets(
            case.request.site_id, case.request.site_version
        )
        state = self.routing.initialise(
            self._case_info(case),
            graph,
            params,
            self._initial_anchors(case),
            case.created_at,
        )
        state.pending_probes = [
            job.payload.probe.model_copy(deep=True)
            for job in self.repository.list_jobs(case.request.case_id)
        ]
        decisions = self._active_decisions(case.request.case_id)
        confirmed = {item.tracklet_id for item in decisions if item.decision == "CONFIRM"}
        rejected = {item.tracklet_id for item in decisions if item.decision == "REJECT"}
        seen_tracklets: set[str] = set()
        effective_now = case.created_at
        last_result: RoutingWorkerResult | None = None
        replayed_inputs: list[str] = []
        for record in ordered:
            payload = dict(record.payload)
            supplied_fingerprint = payload.pop("fingerprint", None)
            if (
                not supplied_fingerprint
                or _fingerprint(payload) != supplied_fingerprint
            ):
                raise ReplayEvidenceIncomplete(
                    f"replay_evidence_incomplete: routing input {record.record_id!r} failed fingerprint verification"
                )
            if (
                payload.get("site_id") != case.request.site_id
                or payload.get("site_version") != case.request.site_version
                or payload.get("params_version") != case.params_version
            ):
                raise ReplayEvidenceIncomplete(
                    "replay_evidence_incomplete: pinned site or params version changed"
                )
            # Historical decisions are superseded by the current immutable
            # decision/revocation ledger. Apply final rejects from the first
            # round, before the beam can discard a correct alternative.
            if payload.get("source_decision_id") is not None:
                continue
            try:
                effective_now = datetime.fromisoformat(str(payload["effective_now"]))
                probe_results = [
                    ProbeResult.model_validate(item)
                    for item in payload.get("probe_results", [])
                ]
                anchors = Anchors.model_validate(payload.get("anchors", {}))
            except Exception as exc:
                raise ReplayEvidenceIncomplete(
                    f"replay_evidence_incomplete: invalid routing input {record.record_id!r}"
                ) from exc
            seen_tracklets.update(
                tracklet.id for result in probe_results for tracklet in result.tracklets
            )
            anchors = anchors.model_copy(update={
                "confirm": sorted(confirmed & seen_tracklets),
                "reject": sorted(rejected),
                "review_only": False,
                "operator_closed": False,
                "replay_evidence_only": True,
            })
            last_result = self.routing.advance(
                state,
                probe_results,
                graph,
                anchors,
                params,
                effective_now,
                floor_assets,
            )
            state = last_result.step_result.state
            replayed_inputs.append(record.record_id)

        if last_result is None:
            raise ReplayEvidenceIncomplete(
                "replay_evidence_incomplete: revocation removed every replayable routing input"
            )
        previous_stop_reason = None
        if case.lifecycle == CaseLifecycle.CLOSED:
            previous_stop_reason = (
                case.latest_route.step_result.stop_reason
                if case.latest_route is not None
                else last_result.step_result.stop_reason
            )
            # Re-render in review-only mode as well: patching the DTO after
            # rendering leaves SVG status/position semantics out of sync.
            state.stop_reason = previous_stop_reason
        last_result = self.routing.advance(
            state, [], graph,
            Anchors(confirm=sorted(confirmed), reject=sorted(rejected),
                    review_only=case.lifecycle == CaseLifecycle.CLOSED),
            params, effective_now, floor_assets,
        )
        if case.lifecycle == CaseLifecycle.CLOSED:
            last_result = last_result.model_copy(update={
                "step_result": last_result.step_result.model_copy(update={
                    "probes": [], "done": True,
                    "mode": SearchMode.PAST_SUMMARY,
                    "stop_reason": previous_stop_reason,
                }, deep=True),
            }, deep=True)
        return self._persist_replayed_routing_result(
            case,
            last_result,
            trigger=trigger,
            replayed_inputs=replayed_inputs,
        )

    def _persist_replayed_routing_result(
        self,
        case: CaseRecord,
        worker_result: RoutingWorkerResult,
        *,
        trigger: RoutingTriggerRecord,
        replayed_inputs: list[str],
    ) -> CaseRecord:
        expected_revision = case.revision
        now = self.clock.now()
        previous_route = case.latest_route
        next_route_revision = max(
            (previous_route.revision + 1 if previous_route is not None else 1),
            worker_result.rendered_route.route_revision,
            (case.routing_state.state_version + 1 if case.routing_state is not None else 1),
        )
        rendered = worker_result.rendered_route.model_copy(
            update={"route_revision": next_route_revision}, deep=True
        )
        step = worker_result.step_result.model_copy(deep=True)
        # Replay omits obsolete decision rounds. Never reuse their state/route
        # versions when the next live probe arrives after a searching review.
        step.state.state_version = next_route_revision
        route_artifacts = self._prepare_route_artifacts(
            case.request.case_id, next_route_revision, rendered.maps
        )
        superseded_resources = self._superseded_route_resources(case)

        case.routing_state = step.state
        case.latest_route = StoredRoute(
            revision=next_route_revision,
            step_result=step,
            rendered=rendered,
            created_at=now,
        )
        case.updated_at = now
        automatic_retention_command = None
        if case.lifecycle == CaseLifecycle.SEARCHING:
            params = self.sites.get_params(case.params_version)
            case.next_heartbeat_at = now + timedelta(seconds=params.heartbeat_s)
            if step.done:
                case.lifecycle = CaseLifecycle.CLOSED
                case.closed_at = now
                case.retention_due_at = now + timedelta(seconds=self.retention_ttl_s)
                automatic_retention_command = RetentionCommand(
                    command_id=self.ids.new("retention", case.request.case_id, "routing-complete", next_route_revision),
                    case_id=case.request.case_id, action="CLOSE_CASE",
                    actor_id="routing-worker", occurred_at=now,
                )
        if case.lifecycle == CaseLifecycle.CLOSED:
            case.live_feed = None
            case.next_heartbeat_at = None
        with self.repository.transaction():
            saved = self.repository.save(case, expected_revision)
            # Publish only after acquiring the case revision. A worker callback
            # can change the case while Routing runs over HTTP; a rejected CAS
            # must not occupy the next immutable route key with stale SVG.
            for item, artifact in zip(rendered.maps, route_artifacts, strict=True):
                self.artifacts.put(
                    artifact["manifest"]["object_key"],
                    item.svg.encode("utf-8"),
                    item.mime_type,
                )
            if saved.lifecycle == CaseLifecycle.SEARCHING:
                self._create_probe_jobs(saved, step.probes)
            if automatic_retention_command is not None:
                self._enqueue(
                    saved.request.case_id, OutboxTopic.RETENTION_COMMAND,
                    f"retention-command:{automatic_retention_command.command_id}",
                    {"command": automatic_retention_command.model_dump(mode="json")},
                )
                self._event(saved.request.case_id, "CASE_CLOSED", actor_id="routing-worker",
                            payload={"reason": step.stop_reason, "retention_command_id": automatic_retention_command.command_id})
            for resource in superseded_resources:
                self._enqueue_retention_resource(resource)
            for artifact in route_artifacts:
                self._record_case_data(
                    saved.request.case_id,
                    CaseDataKind.ARTIFACT,
                    artifact["artifact_id"],
                    artifact,
                )
            self._record_case_data(
                saved.request.case_id,
                CaseDataKind.ROUTING_STATE,
                f"{saved.request.case_id}:state:{step.state.state_version}:replay:{trigger.trigger_id}",
                step.state.model_dump(mode="json"),
            )
            self._record_case_data(
                saved.request.case_id,
                CaseDataKind.ROUTE_REVISION,
                f"{saved.request.case_id}:route:{next_route_revision}",
                {
                    "revision": next_route_revision,
                    "step_result": step.model_dump(mode="json"),
                    "maps": [
                        {
                            "floor_id": item.floor_id,
                            "mime_type": item.mime_type,
                            "width": item.width,
                            "height": item.height,
                            "alt_text": item.alt_text,
                            "object_key": artifact["manifest"]["object_key"],
                        }
                        for item, artifact in zip(rendered.maps, route_artifacts, strict=True)
                    ],
                    "replay_trigger_id": trigger.trigger_id,
                },
            )
            self._record_case_data(
                saved.request.case_id,
                CaseDataKind.ROUTING_RUN,
                trigger.trigger_id,
                {
                    "input_case_revision": expected_revision,
                    "output_case_revision": saved.revision,
                    "route_revision": next_route_revision,
                    "trigger_type": trigger.trigger_type,
                    "source_decision_id": (
                        trigger.dedupe_key.rsplit(":decision:", 1)[1]
                        if ":decision:" in trigger.dedupe_key else None
                    ),
                    "probe_result_ids": [],
                    "anchors": {},
                    "replayed_input_ids": replayed_inputs,
                    "status": "DONE",
                },
            )
            self._event(
                saved.request.case_id,
                "ROUTING_REPLAYED",
                payload={
                    "trigger_id": trigger.trigger_id,
                    "route_revision": next_route_revision,
                    "replayed_input_count": len(replayed_inputs),
                },
            )
        return saved

    def _validated_reference_upload(
        self,
        body: CreateCaseBody,
        owner_id: str,
        *,
        for_update: bool,
    ):
        try:
            if body.reference_upload_id is not None:
                upload = self.repository.get_reference_upload(
                    body.reference_upload_id, for_update=for_update
                )
            else:
                reference = body.reference_image_ref
                if (
                    reference is None
                    or reference.object_key is None
                    or reference.sha256 is None
                ):
                    raise KeyError("missing reference image identity")
                upload = self.repository.get_reference_upload_by_key(
                    reference.object_key, for_update=for_update
                )
        except KeyError as exc:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_NOT_FOUND",
                "reference image upload was not found",
                status=404,
            ) from exc
        if upload.operator_id != owner_id:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_NOT_FOUND",
                "reference image upload was not found",
                status=404,
            )
        if upload.case_id != body.case_id:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_INVALID",
                "reference image upload belongs to a different case",
            )
        if upload.status == ReferenceImageUploadStatus.CONSUMED:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_ALREADY_USED",
                "reference image upload was already consumed",
            )
        if upload.status == ReferenceImageUploadStatus.EXPIRED or (
            upload.expires_at <= self.clock.now()
        ):
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_EXPIRED",
                "reference image upload has expired",
                status=410,
            )
        reference = body.reference_image_ref
        if reference is not None and upload.sha256 != reference.sha256:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_CONTENT_MISMATCH",
                "reference image checksum does not match its upload record",
            )
        try:
            data, mime_type = self.artifacts.get(upload.object_key)
        except KeyError as exc:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_CONTENT_MISMATCH",
                "reference image bytes are missing",
            ) from exc
        if (
            mime_type != "image/jpeg"
            or len(data) != upload.size_bytes
            or hashlib.sha256(data).hexdigest() != upload.sha256
        ):
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_CONTENT_MISMATCH",
                "reference image bytes do not match the upload record",
            )
        return upload

    @staticmethod
    def _with_reference_artifact(
        body: CreateCaseBody, upload
    ) -> CreateCaseBody:
        return body.model_copy(
            update={
                "reference_upload_id": None,
                "reference_image_ref": ArtifactRef(
                    object_key=upload.object_key,
                    sha256=upload.sha256,
                )
            },
            deep=True,
        )

    def create_case(
        self,
        body: CreateCaseBody,
        *,
        idempotency_key: str,
        reference_owner_id: str | None = None,
    ) -> CaseView:
        storage_key = f"case:create:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return self.get_view(str(replay["case_id"]))

        if reference_owner_id is not None:
            upload = self._validated_reference_upload(
                body, reference_owner_id, for_update=False
            )
            body = self._with_reference_artifact(body, upload)

        if body.reference_image_ref is None:
            raise ReferenceUploadError(
                "REFERENCE_UPLOAD_NOT_FOUND",
                "a registered reference image upload is required",
                status=404,
            )

        graph = self.sites.get_graph(body.site_id, body.site_version)
        params = self.sites.get_params()
        now = self.clock.now()
        gallery_command = TargetGalleryCommand(
            command_id=self.ids.new("gallery_cmd", body.case_id, "reference"),
            case_id=body.case_id,
            source="reference",
            image_ref=body.reference_image_ref,
        )
        case_info = CaseInfo(
            case_id=body.case_id,
            target_id=body.target_id,
            start_time=body.last_known.time,
            profile=body.profile,
            window_start=body.window.start,
            window_end=body.window.end,
            last_known_camera=body.last_known.camera_id,
            last_known_zone=body.last_known.zone_id,
            last_known_time=body.last_known.time,
            last_known_bbox_xywh=body.last_known.bbox_xywh,
            last_known_footpoint_px=body.last_known.footpoint_px,
            last_known_map_position=body.last_known.map_position,
        )
        state = self.routing.initialise(
            case_info,
            graph,
            params,
            Anchors(
                last_known_camera=body.last_known.camera_id,
                last_known_zone=body.last_known.zone_id,
                last_known_time=body.last_known.time,
                last_known_bbox_xywh=body.last_known.bbox_xywh,
                last_known_footpoint_px=body.last_known.footpoint_px,
                last_known_map_position=body.last_known.map_position,
            ),
            now,
        )
        case = CaseRecord(
            request=body,
            params_version=params.version,
            lifecycle=CaseLifecycle.PREPARING,
            routing_state=state,
            created_at=now,
            updated_at=now,
            replay_capability="COMPLETE",
        )
        replay_case_id: str | None = None
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                replay_case_id = str(replay["case_id"])
            else:
                upload = None
                if reference_owner_id is not None:
                    upload = self._validated_reference_upload(
                        body, reference_owner_id, for_update=True
                    )
                self.repository.add(case)
                self._event(
                    body.case_id,
                    "CASE_CREATED",
                    actor_id=body.operator_id,
                    payload={
                        "site_id": body.site_id,
                        "site_version": body.site_version,
                        "params_version": params.version,
                        "contracts_version": "javes-boundary.v1",
                    },
                )
                self._enqueue(
                    body.case_id,
                    OutboxTopic.TARGET_GALLERY,
                    f"target-gallery:{gallery_command.command_id}",
                    {"command": gallery_command.model_dump(mode="json")},
                )
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource", body.case_id, "reference-image"
                        ),
                        case_id=body.case_id,
                        kind="REFERENCE_IMAGE",
                        retention_class=RetentionClass.CASE_RESULT,
                        artifact_ref=body.reference_image_ref,
                    )
                )
                self.repository.remember_idempotency(
                    storage_key, fingerprint, {"case_id": body.case_id}
                )
                if upload is not None:
                    upload.status = ReferenceImageUploadStatus.CONSUMED
                    upload.consumed_at = now
                    self.repository.save_reference_upload(upload)
                    self._event(
                        body.case_id,
                        "REFERENCE_IMAGE_UPLOAD_CONSUMED",
                        actor_id=reference_owner_id,
                        payload={"upload_id": upload.upload_id},
                    )
        if replay_case_id is not None:
            return self.get_view(replay_case_id)
        return self.get_view(body.case_id)

    def _routing_tick(
        self,
        case_id: str,
        *,
        probe_results: list[ProbeResult],
        anchors: Anchors,
        trigger_id: str | None = None,
        trigger_type: str = "DIRECT",
        trigger_dedupe_key: str | None = None,
    ) -> CaseRecord:
        case = self.repository.get(case_id)
        if case.routing_state is None:
            raise RuntimeError("case has no Routing state")
        expected_revision = case.revision
        graph = self.sites.get_graph(case.request.site_id, case.request.site_version)
        params = self.sites.get_params(case.params_version)
        now = self.clock.now()
        routing_input_sequence = len(
            self.repository.list_case_data(
                case_id, kinds={CaseDataKind.ROUTING_INPUT.value}
            )
        )
        evidence_resources: list[RetentionResource] = []

        for result in probe_results:
            for tracklet in result.tracklets:
                case.tracklets[tracklet.id] = tracklet.model_dump(
                    mode="python", by_alias=True
                )
                evidence_resources.append(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource", case_id, "tracklet", tracklet.id
                        ),
                        case_id=case_id,
                        kind="TRACKLET_METADATA",
                        retention_class=RetentionClass.EPHEMERAL,
                    )
                )
                for index, path in enumerate(tracklet.crop_paths):
                    evidence_resources.append(
                        RetentionResource(
                            resource_id=self.ids.new(
                                "resource",
                                case_id,
                                "tracklet-crop",
                                tracklet.id,
                                index,
                                path,
                            ),
                            case_id=case_id,
                            kind="TRACKLET_CROP",
                            retention_class=RetentionClass.CASE_RESULT,
                            artifact_ref=self._artifact_ref_from_path(path),
                        )
                    )
                if tracklet.embedding_path:
                    evidence_resources.append(
                        RetentionResource(
                            resource_id=self.ids.new(
                                "resource",
                                case_id,
                                "tracklet-embedding",
                                tracklet.id,
                            ),
                            case_id=case_id,
                            kind="TRACKLET_EMBEDDING",
                            retention_class=RetentionClass.CASE_RESULT,
                            artifact_ref=self._artifact_ref_from_path(
                                tracklet.embedding_path
                            ),
                        )
                    )
            for candidate in result.candidates:
                if candidate.id is not None:
                    case.candidates[candidate.id] = candidate.model_dump(
                        mode="python", by_alias=True
                    )
                    evidence_resources.append(
                        RetentionResource(
                            resource_id=self.ids.new(
                                "resource", case_id, "candidate", candidate.id
                            ),
                            case_id=case_id,
                            kind="CANDIDATE_METADATA",
                            retention_class=RetentionClass.EPHEMERAL,
                        )
                    )

        worker_result = self.routing.advance(
            case.routing_state,
            probe_results,
            graph,
            anchors,
            params,
            now,
            self.sites.get_floor_assets(
                case.request.site_id, case.request.site_version
            ),
        )
        step = worker_result.step_result
        if case.lifecycle != CaseLifecycle.SEARCHING and step.probes:
            step = step.model_copy(update={"probes": []}, deep=True)
        case.routing_state = step.state
        route_revision = worker_result.rendered_route.route_revision
        previous_route = case.latest_route
        route_changed = (
            previous_route is None or previous_route.revision != route_revision
        )
        route_artifacts = (
            self._prepare_route_artifacts(case_id, route_revision, worker_result.rendered_route.maps)
            if route_changed else []
        )
        superseded_route_resources = self._superseded_route_resources(case) if route_changed else []
        case.latest_route = StoredRoute(
            revision=route_revision,
            step_result=step,
            rendered=worker_result.rendered_route,
            created_at=now,
        )
        case.updated_at = now
        case.next_heartbeat_at = (
            now + timedelta(seconds=params.heartbeat_s)
            if case.lifecycle == CaseLifecycle.SEARCHING and not step.done
            else None
        )
        automatic_retention_command: RetentionCommand | None = None
        if step.done and case.lifecycle == CaseLifecycle.SEARCHING:
            case.lifecycle = CaseLifecycle.CLOSED
            case.live_feed = None
            case.closed_at = now
            case.retention_due_at = now + timedelta(seconds=self.retention_ttl_s)
            case.next_heartbeat_at = None
            automatic_retention_command = RetentionCommand(
                command_id=self.ids.new(
                    "retention", case_id, "routing-complete", route_revision
                ),
                case_id=case_id,
                action="CLOSE_CASE",
                actor_id="routing-worker",
                occurred_at=now,
            )

        clip_commands = self._clip_hold_commands(case, probe_results)
        with self.repository.transaction():
            saved = self.repository.save(case, expected_revision)
            if route_changed:
                for rendered, artifact in zip(
                    worker_result.rendered_route.maps, route_artifacts, strict=True
                ):
                    self.artifacts.put(
                        artifact["manifest"]["object_key"],
                        rendered.svg.encode("utf-8"),
                        rendered.mime_type,
                    )
            if saved.lifecycle == CaseLifecycle.SEARCHING:
                self._create_probe_jobs(saved, step.probes)
            for resource in superseded_route_resources:
                self._enqueue_retention_resource(resource)
            for resource in evidence_resources:
                self._enqueue_retention_resource(resource)
            for command in clip_commands:
                self._enqueue(
                    case_id,
                    OutboxTopic.CLIP_CUT,
                    f"clip-cut:{command.clip_request_id}",
                    {"command": command.model_dump(mode="json")},
                )
            if automatic_retention_command is not None:
                self._enqueue(
                    case_id,
                    OutboxTopic.RETENTION_COMMAND,
                    f"retention-command:{automatic_retention_command.command_id}",
                    {"command": automatic_retention_command.model_dump(mode="json")},
                )
                self._event(
                    case_id,
                    "CASE_CLOSED",
                    actor_id="routing-worker",
                    payload={
                        "retention_command_id": automatic_retention_command.command_id,
                        "reason": step.stop_reason,
                    },
                )
            if route_changed:
                for artifact in route_artifacts:
                    self._record_case_data(
                        case_id,
                        CaseDataKind.ARTIFACT,
                        artifact["artifact_id"],
                        artifact,
                    )
            for result in probe_results:
                for tracklet in result.tracklets:
                    self._record_case_data(
                        case_id,
                        CaseDataKind.TRACKLET,
                        f"{tracklet.id}:{_fingerprint(tracklet)}",
                        tracklet.model_dump(mode="json", by_alias=True),
                    )
                for candidate in result.candidates:
                    if candidate.id is not None:
                        self._record_case_data(
                            case_id,
                            CaseDataKind.CANDIDATE,
                            f"{candidate.id}:{_fingerprint(candidate)}",
                            candidate.model_dump(mode="json", by_alias=True),
                        )
            self._record_case_data(
                case_id,
                CaseDataKind.ROUTING_STATE,
                f"{case_id}:state:{step.state.state_version}",
                step.state.model_dump(mode="json"),
            )
            if route_changed:
                self._record_case_data(
                    case_id,
                    CaseDataKind.ROUTE_REVISION,
                    f"{case_id}:route:{route_revision}",
                    {
                        "revision": route_revision,
                        "step_result": step.model_dump(mode="json"),
                        "maps": [
                            {
                                "floor_id": item.floor_id,
                                "mime_type": item.mime_type,
                                "width": item.width,
                                "height": item.height,
                                "alt_text": item.alt_text,
                                "object_key": artifact["manifest"]["object_key"],
                            }
                            for item, artifact in zip(worker_result.rendered_route.maps, route_artifacts, strict=True)
                        ],
                    },
                )
            run_id = trigger_id or self.ids.new(
                "routing_run",
                case_id,
                expected_revision,
                route_revision,
                [item.probe_id for item in probe_results],
                anchors.model_dump(mode="json"),
            )
            source_decision_id = None
            if trigger_type == "OPERATOR_DECISION" and trigger_dedupe_key:
                marker = ":decision:"
                if marker in trigger_dedupe_key:
                    source_decision_id = trigger_dedupe_key.rsplit(marker, 1)[1]
            routing_input = {
                "sequence": routing_input_sequence,
                "trigger_id": run_id,
                "trigger_type": trigger_type,
                "trigger_dedupe_key": trigger_dedupe_key,
                "source_decision_id": source_decision_id,
                "effective_now": now.isoformat(),
                "site_id": case.request.site_id,
                "site_version": case.request.site_version,
                "params_version": case.params_version,
                "contracts_version": "javes-boundary.v1",
                "input_case_revision": expected_revision,
                "output_case_revision": saved.revision,
                "output_route_revision": route_revision,
                "probe_results": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in probe_results
                ],
                "anchors": anchors.model_dump(mode="json"),
                "active_decision_ids": [
                    item.decision_id for item in self._active_decisions(case_id)
                ],
            }
            routing_input["fingerprint"] = _fingerprint(routing_input)
            self._record_case_data(
                case_id,
                CaseDataKind.ROUTING_INPUT,
                run_id,
                routing_input,
                schema_version="routing-input.v1",
                occurred_at=now,
            )
            self._record_case_data(
                case_id,
                CaseDataKind.ROUTING_RUN,
                run_id,
                {
                    "input_case_revision": expected_revision,
                    "output_case_revision": saved.revision,
                    "route_revision": route_revision,
                    "trigger_type": trigger_type,
                    "probe_result_ids": [item.probe_id for item in probe_results],
                    "anchors": anchors.model_dump(mode="json"),
                    "status": "DONE",
                },
            )
            self._event(
                case_id,
                "ROUTING_ADVANCED",
                payload={
                    "route_revision": route_revision,
                    "mode": step.mode.value,
                    "probe_ids": [item.probe_id for item in step.probes],
                    "result_probe_ids": [item.probe_id for item in probe_results],
                },
            )
        return saved

    def _create_probe_jobs(self, case: CaseRecord, probes) -> None:
        if case.gallery_ref is None:
            raise RuntimeError("case target gallery is not ready")
        now = self.clock.now()
        for probe in probes:
            job_id = self.ids.new("job", case.request.case_id, probe.probe_id)
            try:
                existing = self.repository.get_job(job_id)
            except KeyError:
                existing = None
            if existing is not None:
                requested = (probe.camera, probe.t_from, probe.t_to, probe.retry_count, probe.retry_of)
                original = existing.payload.probe
                identity = (original.camera, original.t_from, original.t_to, original.retry_count, original.retry_of)
                if requested != identity:
                    raise ValueError("probe id reused for a different physical request")
                # Delivery already owns an immutable gallery/deadline. Review
                # priorities or hypothesis IDs do not create another CV task.
                continue
            job = JobRecord(
                job_id=job_id,
                case_id=case.request.case_id,
                dedupe_key=f"perception:{case.request.case_id}:{probe.probe_id}",
                payload=PerceptionProbeJob(
                    job_id=job_id,
                    case_id=case.request.case_id,
                    target_gallery_ref=case.gallery_ref,
                    probe=probe,
                    deadline=(
                        None
                        if probe.t_to is None
                        else now + timedelta(seconds=self.job_deadline_s)
                    ),
                ),
                created_at=now,
                updated_at=now,
            )
            self.repository.add_job(job)
            self._record_case_data(
                case.request.case_id,
                CaseDataKind.PROBE_REQUEST,
                probe.probe_id,
                {
                    "job_id": job_id,
                    "probe": probe.model_dump(mode="json", by_alias=True),
                },
            )

    def _clip_hold_commands(
        self, case: CaseRecord, probe_results: list[ProbeResult],
        *, confirmed_tracklet_ids: set[str] | None = None,
    ) -> list[CutClipCommand]:
        if case.latest_route is None:
            return []
        surviving = {
            tracklet_id
            for hypothesis in case.latest_route.step_result.hypotheses
            for visit in hypothesis.visits
            for tracklet_id in visit.tracklet_ids
        }
        surviving.update(confirmed_tracklet_ids or set())
        tracklet_models = {
            tracklet.id: tracklet
            for result in probe_results
            for tracklet in result.tracklets
        }
        commands: list[CutClipCommand] = []
        for result in probe_results:
            for candidate in result.candidates:
                if (
                    candidate.id is None
                    or candidate.id in case.candidate_to_clip
                    or candidate.tracklet_id not in surviving
                ):
                    continue
                tracklet = tracklet_models.get(candidate.tracklet_id)
                if tracklet is None or tracklet.clip_ref is None:
                    continue
                source = ArtifactRef.model_validate(tracklet.clip_ref)
                # Context padding must stay inside the requested recording
                # window. In particular, the first/last NVIDIA frames cannot
                # provide context footage beyond that window.
                clip_from = max(
                    case.request.window.start,
                    tracklet.t_in - timedelta(seconds=self.clip_padding_s),
                )
                clip_to = min(
                    case.request.window.end,
                    tracklet.t_out + timedelta(seconds=self.clip_padding_s),
                )
                commands.append(
                    CutClipCommand(
                        clip_request_id=self.ids.new(
                            "clip_req", case.request.case_id, candidate.id
                        ),
                        case_id=case.request.case_id,
                        candidate_id=candidate.id,
                        tracklet_id=tracklet.id,
                        camera_id=tracklet.camera,
                        t_from=clip_from,
                        t_to=clip_to,
                        padding_before_s=max(0.0, (tracklet.t_in - clip_from).total_seconds()),
                        padding_after_s=max(0.0, (clip_to - tracklet.t_out).total_seconds()),
                        source_ref=source,
                    )
                )
        return commands

    def submit_probe_result(self, job_id: str, result: ProbeResult) -> CaseView:
        with self.repository.transaction():
            job = self.repository.get_job(job_id)
            if result.probe_id != job.payload.probe.probe_id:
                raise ValueError("ProbeResult probe_id does not match job")
            clean_result = self._sanitize_probe_result(job, result)
            # Probe IDs are opaque and are not required to be globally unique
            # across cases. Scope the delivery fingerprint to its durable job.
            fingerprint = _fingerprint(
                {"job_id": job_id, "result": result.model_dump(mode="json")}
            )
            accepted = self.repository.add_probe_result(
                ProbeResultRecord(
                    fingerprint=fingerprint,
                    job_id=job_id,
                    case_id=job.case_id,
                    result=result,
                    accepted_at=self.clock.now(),
                )
            )
            if not accepted:
                self._event(
                    job.case_id,
                    "WORKER_RESULT_DUPLICATE",
                    payload={"job_id": job_id, "fingerprint": fingerprint},
                )
                return self.get_view(job.case_id)
            if job.payload.probe.t_to is not None and job.result_fingerprints:
                raise ValueError("finite job already has a different result")

            job.result_fingerprints = sorted(
                set(job.result_fingerprints + [fingerprint])
            )
            job.updated_at = self.clock.now()
            if result.status == ProbeStatus.FAILED:
                job.state = JobState.FAILED
            elif result.status == ProbeStatus.TIMEOUT:
                job.state = JobState.TIMEOUT
            elif result.status == ProbeStatus.UNAVAILABLE:
                job.state = JobState.UNAVAILABLE
            elif job.payload.probe.t_to is None and not result.stream_closed:
                job.state = JobState.LEASED
            else:
                job.state = JobState.DONE
            job.lease_owner = None if job.state != JobState.LEASED else job.lease_owner
            job.lease_expires_at = (
                None if job.state != JobState.LEASED else job.lease_expires_at
            )
            self.repository.save_job(job)
            self._event(
                job.case_id,
                "PROBE_RESULT_ACCEPTED",
                payload={
                    "job_id": job_id,
                    "probe_id": result.probe_id,
                    "status": result.status.value,
                    "fingerprint": fingerprint,
                },
            )
            self._enqueue_routing(
                job.case_id,
                "PROBE_RESULT",
                f"probe-result:{fingerprint}",
                probe_results=[clean_result],
            )
        return self.get_view(job.case_id)

    @staticmethod
    def _sanitize_probe_result(job: JobRecord, result: ProbeResult) -> ProbeResult:
        """Keep the raw accepted result, but route only safe correlated evidence."""

        if result.status != ProbeStatus.DONE:
            if result.tracklets or result.candidates:
                logger.warning(
                    "Ignoring evidence attached to non-DONE probe %s", result.probe_id
                )
            return result.model_copy(
                update={"tracklets": [], "candidates": []}, deep=True
            )
        valid_tracklets = []
        seen_tracklets: set[str] = set()
        for tracklet in sorted(result.tracklets, key=lambda item: item.id):
            valid = (
                tracklet.id not in seen_tracklets
                and tracklet.camera == job.payload.probe.camera
                and tracklet.case_id in {None, job.case_id}
                and tracklet.probe_id in {None, result.probe_id}
            )
            if not valid:
                logger.warning(
                    "Skipping uncorrelated/duplicate tracklet %s for job %s",
                    tracklet.id,
                    job.job_id,
                )
                continue
            seen_tracklets.add(tracklet.id)
            valid_tracklets.append(tracklet)
        valid_candidates = []
        seen_candidates: set[str] = set()
        for candidate in sorted(
            result.candidates,
            key=lambda item: (item.id or "", item.tracklet_id),
        ):
            candidate_key = candidate.id or f"tracklet:{candidate.tracklet_id}"
            if (
                candidate.tracklet_id not in seen_tracklets
                or candidate_key in seen_candidates
            ):
                logger.warning(
                    "Skipping uncorrelated/duplicate candidate %s for job %s",
                    candidate.id,
                    job.job_id,
                )
                continue
            seen_candidates.add(candidate_key)
            valid_candidates.append(candidate)
        return result.model_copy(
            update={
                "tracklets": valid_tracklets,
                "candidates": valid_candidates,
            },
            deep=True,
        )

    def run_pending_jobs(
        self, case_id: str | None = None, *, limit: int | None = None
    ) -> list[str]:
        now = self.clock.now()
        self.repository.reclaim_expired_jobs(now=now)
        jobs = self.repository.claim_jobs(
            worker_id="in-process-perception",
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
            limit=limit if limit is not None else 100,
            case_id=case_id,
        )
        processed: list[str] = []
        for job in jobs:
            current_time = self.clock.now()
            if (
                job.payload.deadline is not None
                and job.payload.deadline <= current_time
            ):
                result = ProbeResult(
                    probe_id=job.payload.probe.probe_id,
                    status=ProbeStatus.TIMEOUT,
                    coverage_ratio=0.0,
                    error_code="JOB_DEADLINE_EXPIRED",
                )
            else:
                result = self.perception.execute(job.payload, current_time)
            self.submit_probe_result(job.job_id, result)
            processed.append(job.job_id)
        return processed

    def run_until_quiescent(self, case_id: str, *, max_cycles: int = 100) -> int:
        total = 0
        for _ in range(max_cycles):
            processed = self.run_pending_jobs(case_id)
            total += len(processed)
            if not processed:
                return total
        raise RuntimeError("fake job loop did not become quiescent")

    def poll_open_jobs(self, case_id: str | None = None) -> list[str]:
        processed: list[str] = []
        cases = (
            [case_id]
            if case_id is not None
            else [
                item.request.case_id
                for item in self.repository.list_cases(
                    lifecycles={CaseLifecycle.SEARCHING.value}
                )
            ]
        )
        for current_case_id in cases:
            for job in self.repository.list_jobs(
                current_case_id, states={JobState.LEASED.value}
            ):
                if job.payload.probe.t_to is not None:
                    continue
                now = self.clock.now()
                job.lease_expires_at = now + timedelta(seconds=30)
                job.updated_at = now
                self.repository.save_job(job)
                result = self.perception.execute(job.payload, now)
                self.submit_probe_result(job.job_id, result)
                processed.append(job.job_id)
        return processed

    def heartbeat(self, case_id: str) -> CaseView:
        case = self.repository.get(case_id)
        if case.lifecycle != CaseLifecycle.SEARCHING:
            return self.get_view(case_id)
        due = case.next_heartbeat_at or self.clock.now()
        with self.repository.transaction():
            self._enqueue_routing(
                case_id,
                "HEARTBEAT",
                f"heartbeat:{case_id}:{due.isoformat()}",
            )
        return self.get_view(case_id)

    def submit_live_feed(self, update: LiveFeedUpdate) -> CaseView:
        """Accept latest live metadata without exposing the underlying stream ref."""

        case = self.repository.get(update.case_id)
        graph = self.sites.get_graph(case.request.site_id, case.request.site_version)
        if update.camera_id not in graph.cameras:
            raise ValueError(f"unknown live camera {update.camera_id!r}")
        with self.repository.transaction():
            case = self.repository.get(update.case_id)
            if case.lifecycle != CaseLifecycle.SEARCHING:
                raise ValueError(
                    f"cannot update live feed while case is {case.lifecycle.value}"
                )
            current = case.live_feed
            if current is not None and current.update_id == update.update_id:
                if current != update:
                    raise ValueError("live update_id reused with different payload")
                return self.get_view(update.case_id)
            if current is not None and (
                update.observed_at,
                update.update_id,
            ) <= (
                current.observed_at,
                current.update_id,
            ):
                return self.get_view(update.case_id)
            expected = case.revision
            case.live_feed = update.model_copy(deep=True)
            case.updated_at = self.clock.now()
            self.repository.save(case, expected)
            self._enqueue_retention_resource(
                RetentionResource(
                    resource_id=self.ids.new(
                        "resource", update.case_id, "live", update.update_id
                    ),
                    case_id=update.case_id,
                    kind="LIVE_BUFFER",
                    retention_class=RetentionClass.EPHEMERAL,
                    artifact_ref=(
                        ArtifactRef(object_key=update.stream_ref)
                        if not urlparse(update.stream_ref).scheme
                        else ArtifactRef(uri=update.stream_ref)
                    ),
                    expires_at=update.expires_at,
                )
            )
        return self.get_view(update.case_id)

    def get_live_stream(self, case_id: str) -> tuple[bytes, str]:
        case = self.repository.get(case_id)
        if case.lifecycle != CaseLifecycle.SEARCHING or case.live_feed is None:
            raise KeyError("live stream is not available")
        if (
            case.live_feed.expires_at is not None
            and case.live_feed.expires_at <= self.clock.now()
        ):
            raise KeyError("live stream has expired")
        return self.live_feed.get(case.live_feed.stream_ref)

    def decide(
        self,
        case_id: str,
        body: ReviewDecisionBody,
        *,
        idempotency_key: str,
    ) -> CaseView:
        storage_key = f"case:{case_id}:decision:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return self.get_view(case_id)
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                return self.get_view(case_id)
            case = self.repository.get(case_id)
            if case.lifecycle not in {CaseLifecycle.SEARCHING, CaseLifecycle.CLOSED}:
                raise ValueError(
                    f"cannot review candidates while case is {case.lifecycle.value}"
                )
            candidate = case.candidates.get(body.candidate_id)
            if candidate is None or candidate.get("tracklet_id") != body.tracklet_id:
                raise KeyError("unknown candidate/tracklet pair")
            prior_decisions = [
                item
                for item in self._active_decisions(case_id)
                if item.tracklet_id == body.tracklet_id
            ]
            if any(item.decision != body.decision for item in prior_decisions):
                raise ValueError(
                    "contradictory review requires the future revoke workflow"
                )
            if prior_decisions:
                self.repository.remember_idempotency(
                    storage_key, fingerprint, {"case_id": case_id}
                )
                return self.get_view(case_id)
            anchors = Anchors(
                confirm_tracklet_ids=(
                    [body.tracklet_id] if body.decision == "CONFIRM" else []
                ),
                reject_tracklet_ids=(
                    [body.tracklet_id] if body.decision == "REJECT" else []
                ),
            )
            searching = case.lifecycle == CaseLifecycle.SEARCHING
            expected = case.revision
            pending = (
                case.pending_confirm_tracklet_ids
                if body.decision == "CONFIRM"
                else case.pending_reject_tracklet_ids
            )
            if body.tracklet_id not in pending:
                pending.append(body.tracklet_id)
                pending.sort()
            case.updated_at = self.clock.now()
            case = self.repository.save(case, expected)
            self._enqueue_routing(
                case_id,
                "OPERATOR_DECISION",
                f"decision:{body.decision_id}",
                anchors=anchors,
            )
            if body.decision == "CONFIRM" and searching:
                confirmed_tracklets = sorted({
                    item.tracklet_id for item in self._active_decisions(case_id)
                    if item.decision == "CONFIRM"
                } | {body.tracklet_id})
                gallery_command = TargetGalleryCommand(
                    command_id=self.ids.new(
                        "gallery_cmd", case_id, "confirmed", body.tracklet_id
                    ),
                    case_id=case_id,
                    source="rebuild",
                    image_ref=case.request.reference_image_ref,
                    tracklet_ids=confirmed_tracklets,
                    base_gallery_ref=case.gallery_ref,
                    model_version=(
                        case.gallery_model_version or "osnet_ain_x1_0_msmt17"
                    ),
                )
                self._enqueue(
                    case_id,
                    OutboxTopic.TARGET_GALLERY,
                    f"target-gallery:{gallery_command.command_id}",
                    {"command": gallery_command.model_dump(mode="json")},
                )
            clip_id = case.candidate_to_clip.get(body.candidate_id)
            if clip_id is None and body.decision == "CONFIRM":
                tracklet = Tracklet.model_validate(case.tracklets[body.tracklet_id])
                evidence = ProbeResult(
                    probe_id=tracklet.probe_id or "review-evidence",
                    status=ProbeStatus.DONE,
                    tracklets=[tracklet],
                    candidates=[Candidate.model_validate(candidate)],
                )
                for cut in self._clip_hold_commands(
                    case, [evidence], confirmed_tracklet_ids={body.tracklet_id}
                ):
                    self._enqueue(
                        case_id, OutboxTopic.CLIP_CUT,
                        f"clip-cut:{cut.clip_request_id}",
                        {"command": cut.model_dump(mode="json")},
                    )
            if clip_id is not None:
                command = ClipStateCommand(
                    command_id=self.ids.new("clip_state", body.decision_id),
                    case_id=case_id,
                    clip_id=clip_id,
                    action="CONFIRM" if body.decision == "CONFIRM" else "RELEASE",
                    operator_id=body.operator_id,
                    occurred_at=body.occurred_at,
                )
                self._enqueue(
                    case_id,
                    OutboxTopic.CLIP_STATE,
                    f"clip-state:{command.command_id}",
                    {"command": command.model_dump(mode="json")},
                )
            self._event(
                case_id,
                "CANDIDATE_DECIDED",
                actor_id=body.operator_id,
                payload=body.model_dump(mode="json"),
            )
            self._record_case_data(
                case_id,
                CaseDataKind.OPERATOR_DECISION,
                body.decision_id,
                body.model_dump(mode="json"),
            )
            self.repository.remember_idempotency(
                storage_key, fingerprint, {"case_id": case_id}
            )
        return self.get_view(case_id)

    def revoke_decision(
        self,
        case_id: str,
        body: RevokeDecisionBody,
        *,
        idempotency_key: str,
    ) -> CaseView:
        storage_key = f"case:{case_id}:decision-revoke:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return self.get_view(case_id)
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                return self.get_view(case_id)
            case = self.repository.get(case_id)
            if case.lifecycle not in {CaseLifecycle.SEARCHING, CaseLifecycle.CLOSED}:
                raise ValueError(
                    f"cannot revoke decisions while case is {case.lifecycle.value}"
                )
            if (
                body.expected_case_revision is not None
                and body.expected_case_revision != case.revision
            ):
                raise ValueError(
                    "expected_case_revision does not match current case revision"
                )
            if (
                case.replay_capability != "COMPLETE"
                or not self.repository.list_case_data(
                    case_id, kinds={CaseDataKind.ROUTING_INPUT.value}
                )
            ):
                raise ReplayEvidenceIncomplete(
                    "replay_evidence_incomplete: decision cannot be revoked safely"
                )
            history = {
                item.decision_id: item for item in self._decision_history(case_id)
            }
            decision = history.get(body.decision_id)
            if decision is None:
                raise KeyError(f"unknown decision {body.decision_id!r}")
            if not decision.active:
                raise ValueError("decision is already revoked")

            expected_revision = case.revision
            case.updated_at = self.clock.now()
            if decision.decision == "CONFIRM":
                if decision.tracklet_id not in case.pending_reject_tracklet_ids:
                    case.pending_reject_tracklet_ids.append(decision.tracklet_id)
                    case.pending_reject_tracklet_ids.sort()
                case.pending_confirm_tracklet_ids = [
                    item
                    for item in case.pending_confirm_tracklet_ids
                    if item != decision.tracklet_id
                ]
            else:
                case.pending_reject_tracklet_ids = [
                    item
                    for item in case.pending_reject_tracklet_ids
                    if item != decision.tracklet_id
                ]
            saved = self.repository.save(case, expected_revision)
            self._record_case_data(
                case_id,
                CaseDataKind.OPERATOR_DECISION_REVOCATION,
                body.revocation_id,
                body.model_dump(mode="json"),
                schema_version="operator-decision-revocation.v1alpha1",
                occurred_at=body.occurred_at,
            )

            remaining = self._active_decisions(case_id)
            clip_id = case.candidate_to_clip.get(decision.candidate_id)
            another_confirm = any(
                item.decision == "CONFIRM" and item.tracklet_id == decision.tracklet_id
                for item in remaining
            )
            if clip_id is not None and not another_confirm:
                clip_action = "RELEASE" if decision.decision == "CONFIRM" else "HOLD"
                clip_command = ClipStateCommand(
                    command_id=self.ids.new(
                        "clip_state", body.revocation_id, clip_action
                    ),
                    case_id=case_id,
                    clip_id=clip_id,
                    action=clip_action,
                    operator_id=body.operator_id,
                    occurred_at=body.occurred_at,
                )
                self._enqueue(
                    case_id,
                    OutboxTopic.CLIP_STATE,
                    f"clip-state:{clip_command.command_id}",
                    {"command": clip_command.model_dump(mode="json")},
                )

            if (
                decision.decision == "CONFIRM"
                and saved.lifecycle == CaseLifecycle.SEARCHING
            ):
                confirmed_tracklets = sorted(
                    {
                        item.tracklet_id
                        for item in remaining
                        if item.decision == "CONFIRM"
                    }
                )
                gallery_command = TargetGalleryCommand(
                    command_id=self.ids.new(
                        "gallery_cmd", case_id, "rebuild", body.revocation_id
                    ),
                    case_id=case_id,
                    source="rebuild",
                    image_ref=case.request.reference_image_ref,
                    tracklet_ids=confirmed_tracklets,
                    base_gallery_ref=case.gallery_ref,
                    model_version=(
                        case.gallery_model_version or "osnet_ain_x1_0_msmt17"
                    ),
                )
                self._enqueue(
                    case_id,
                    OutboxTopic.TARGET_GALLERY,
                    f"target-gallery:{gallery_command.command_id}",
                    {"command": gallery_command.model_dump(mode="json")},
                )

            self._enqueue_routing(
                case_id,
                "DECISION_REVOKE",
                f"decision-revoke:{body.revocation_id}",
            )
            self._event(
                case_id,
                "DECISION_REVOKED",
                actor_id=body.operator_id,
                payload={
                    "revocation_id": body.revocation_id,
                    "decision_id": body.decision_id,
                    "candidate_id": decision.candidate_id,
                    "tracklet_id": decision.tracklet_id,
                    "reason": body.reason,
                },
            )
            self.repository.remember_idempotency(
                storage_key, fingerprint, {"case_id": case_id}
            )
        return self.get_view(saved.request.case_id)

    def command(
        self,
        case_id: str,
        body: CaseCommandBody,
        *,
        idempotency_key: str,
    ) -> CaseView:
        storage_key = f"case:{case_id}:command:{body.action}:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return self.get_view(case_id)
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                return self.get_view(case_id)
            if body.action == "CLOSE":
                self._close(case_id, body)
            elif body.action == "EXPORT":
                self._export(case_id, body)
            elif body.action == "DELETE":
                self._delete(case_id, body)
            else:  # Pydantic prevents this; keep the application boundary explicit.
                raise ValueError(f"unsupported case command {body.action!r}")
            self.repository.remember_idempotency(
                storage_key, fingerprint, {"case_id": case_id}
            )
        return self.get_view(case_id)

    def _close(self, case_id: str, body: CaseCommandBody) -> None:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise ValueError("deleted case cannot be closed")
        if case.lifecycle in {CaseLifecycle.CLOSED, CaseLifecycle.SAVED}:
            return
        if case.lifecycle == CaseLifecycle.EXPORTING:
            raise ValueError("cannot close a case while export is in progress")
        if case.lifecycle == CaseLifecycle.SEARCHING:
            self._enqueue_routing(
                case_id,
                "OPERATOR_CLOSE",
                f"close:{body.command_id}",
                anchors=Anchors(operator_closed=True),
            )
        now = self.clock.now()
        for job in self.repository.list_jobs(
            case_id, states={JobState.PENDING.value, JobState.LEASED.value}
        ):
            if job.state == JobState.LEASED or job.payload.probe.t_to is None:
                self._enqueue(
                    case_id,
                    OutboxTopic.PERCEPTION_CANCEL,
                    f"perception-cancel:{job.job_id}",
                    {"job": job.payload.model_dump(mode="json")},
                )
            job.state = JobState.CANCELLED
            job.updated_at = now
            self.repository.save_job(job)
        case = self.repository.get(case_id)
        expected = case.revision
        case.lifecycle = CaseLifecycle.CLOSED
        case.live_feed = None
        case.closed_at = now
        case.retention_due_at = now + timedelta(seconds=self.retention_ttl_s)
        case.next_heartbeat_at = None
        case.updated_at = now
        self.repository.save(case, expected)
        command = RetentionCommand(
            command_id=self.ids.new("retention", case_id, body.command_id),
            case_id=case_id,
            action="CLOSE_CASE",
            actor_id=body.operator_id,
            occurred_at=body.occurred_at,
        )
        self._enqueue(
            case_id,
            OutboxTopic.RETENTION_COMMAND,
            f"retention-command:{command.command_id}",
            {"command": command.model_dump(mode="json")},
        )
        self._event(
            case_id,
            "CASE_CLOSED",
            actor_id=body.operator_id,
            payload={"retention_command_id": command.command_id},
        )

    def _ordered_confirmed_clips(self, case: CaseRecord) -> list[str]:
        if case.latest_route is None:
            return []
        best_value = case.latest_route.step_result.best
        best_id = best_value.id if isinstance(best_value, Hypothesis) else best_value
        best = next(
            (
                item
                for item in case.latest_route.step_result.hypotheses
                if item.id == best_id
            ),
            None,
        )
        if best is None:
            return []
        clip_by_tracklet = {
            clip.tracklet_id: clip
            for clip in case.clips.values()
            if clip.state == ClipLifecycleState.CONFIRMED
            and clip.status == ClipProcessingStatus.READY
        }
        ordered: list[str] = []
        for visit in sorted(best.visits, key=lambda item: (item.t_in, item.t_out, item.id)):
            for tracklet_id in sorted(visit.tracklet_ids, key=lambda value: (
                case.tracklets.get(value, {}).get("t_start", ""), value
            )):
                clip = clip_by_tracklet.get(tracklet_id)
                if clip is not None and clip.clip_id not in ordered:
                    ordered.append(clip.clip_id)
        return ordered

    def _export(self, case_id: str, body: CaseCommandBody) -> None:
        case = self.repository.get(case_id)
        if case.lifecycle not in {CaseLifecycle.CLOSED, CaseLifecycle.SAVED}:
            raise ValueError(f"cannot export case while it is {case.lifecycle.value}")
        if any(
            item.state.value in {"PENDING", "LEASED", "FAILED"}
            and item.trigger_type in {"OPERATOR_DECISION", "DECISION_REVOKE"}
            for item in self.repository.list_routing_triggers(case_id=case_id)
        ):
            raise ValueError("route recalculation must complete before clip export")
        ordered = self._ordered_confirmed_clips(case)
        if not ordered:
            raise ValueError("case has no confirmed clips in route order")
        confirmed = {
            item.tracklet_id for item in self._active_decisions(case_id)
            if item.decision == "CONFIRM"
        }
        exported_tracklets = {case.clips[clip_id].tracklet_id for clip_id in ordered}
        if not confirmed.issubset(exported_tracklets):
            raise ValueError("all confirmed observations need ready clips on the current route before export")
        command = ExportClipsCommand(
            export_request_id=self.ids.new("export", case_id, body.command_id),
            case_id=case_id,
            ordered_clip_ids=ordered,
        )
        expected = case.revision
        case.lifecycle = CaseLifecycle.EXPORTING
        case.updated_at = self.clock.now()
        self.repository.save(case, expected)
        self._enqueue(
            case_id,
            OutboxTopic.CLIP_EXPORT,
            f"clip-export:{command.export_request_id}",
            {"command": command.model_dump(mode="json")},
        )
        self._event(
            case_id,
            "CASE_EXPORT_REQUESTED",
            actor_id=body.operator_id,
            payload={"export_request_id": command.export_request_id},
        )

    def _delete(self, case_id: str, body: CaseCommandBody) -> None:
        case = self.repository.get(case_id)
        if case.lifecycle == CaseLifecycle.DELETED:
            return
        now = self.clock.now()
        for job in self.repository.list_jobs(
            case_id, states={JobState.PENDING.value, JobState.LEASED.value}
        ):
            if job.state == JobState.LEASED or job.payload.probe.t_to is None:
                self._enqueue(
                    case_id,
                    OutboxTopic.PERCEPTION_CANCEL,
                    f"perception-cancel:{job.job_id}",
                    {"job": job.payload.model_dump(mode="json")},
                )
            job.state = JobState.CANCELLED
            job.updated_at = now
            self.repository.save_job(job)
        expected = case.revision
        case.lifecycle = CaseLifecycle.DELETE_REQUESTED
        case.pending_retention_action = "DELETE_CASE"
        case.live_feed = None
        case.next_heartbeat_at = None
        case.updated_at = now
        self.repository.save(case, expected)
        active_holds = sorted(
            hold.hold_id for hold in case.legal_holds.values() if hold.active
        )
        if active_holds:
            self._event(
                case_id,
                "CASE_DELETE_BLOCKED_BY_LEGAL_HOLD",
                actor_id=body.operator_id,
                payload={"active_hold_ids": active_holds},
            )
        else:
            command = RetentionCommand(
                command_id=self.ids.new("retention", case_id, body.command_id),
                case_id=case_id,
                action="DELETE_CASE",
                actor_id=body.operator_id,
                occurred_at=body.occurred_at,
            )
            self._enqueue(
                case_id,
                OutboxTopic.RETENTION_COMMAND,
                f"retention-command:{command.command_id}",
                {"command": command.model_dump(mode="json")},
            )
            self._event(
                case_id,
                "CASE_DELETE_REQUESTED",
                actor_id=body.operator_id,
                payload={"retention_command_id": command.command_id},
            )

    def enqueue_due_retention(self) -> int:
        """Schedule case-level TTL expiry exactly once per retained case."""

        now = self.clock.now()
        scheduled = 0
        for case in self.repository.list_cases(
            lifecycles={
                CaseLifecycle.CLOSED.value,
                CaseLifecycle.SAVED.value,
            }
        ):
            if case.retention_due_at is None or case.retention_due_at > now:
                continue
            command = RetentionCommand(
                command_id=self.ids.new(
                    "retention", case.request.case_id, "ttl", case.retention_due_at
                ),
                case_id=case.request.case_id,
                action="TTL_EXPIRED",
                actor_id="retention-scheduler",
                occurred_at=case.retention_due_at,
            )
            with self.repository.transaction():
                current = self.repository.get(case.request.case_id)
                if current.lifecycle not in {
                    CaseLifecycle.CLOSED,
                    CaseLifecycle.SAVED,
                }:
                    continue
                if current.retention_due_at is None or current.retention_due_at > now:
                    continue
                expected = current.revision
                current.lifecycle = CaseLifecycle.DELETE_REQUESTED
                current.pending_retention_action = "TTL_EXPIRED"
                current.updated_at = now
                self.repository.save(current, expected)
                active_holds = sorted(
                    hold.hold_id
                    for hold in current.legal_holds.values()
                    if hold.active
                )
                if active_holds:
                    self._event(
                        current.request.case_id,
                        "CASE_DELETE_BLOCKED_BY_LEGAL_HOLD",
                        actor_id="retention-scheduler",
                        payload={"active_hold_ids": active_holds},
                    )
                else:
                    self._enqueue(
                        current.request.case_id,
                        OutboxTopic.RETENTION_COMMAND,
                        f"retention-command:{command.command_id}",
                        {"command": command.model_dump(mode="json")},
                    )
                    self._event(
                        current.request.case_id,
                        "CASE_RETENTION_EXPIRED",
                        actor_id="retention-scheduler",
                        payload={"retention_command_id": command.command_id},
                    )
            scheduled += 1
        return scheduled

    def _redact_deleted_case(self, case: CaseRecord, *, now: datetime) -> None:
        """Remove case content; append-only audit events remain untouched."""

        case.request.target_id = "__deleted__"
        case.request.reference_image_ref = ArtifactRef(object_key="__deleted__")
        case.request.last_known.camera_id = (
            "__deleted__" if case.request.last_known.camera_id else None
        )
        case.request.last_known.zone_id = (
            "__deleted__" if case.request.last_known.zone_id else None
        )
        case.routing_state = None
        case.latest_route = None
        case.gallery_ref = None
        case.gallery_model_version = None
        case.tracklets = {}
        case.candidates = {}
        case.clips = {}
        case.candidate_to_clip = {}
        case.pending_confirm_tracklet_ids = []
        case.pending_reject_tracklet_ids = []
        case.export_ref = None
        case.live_feed = None
        case.content_redacted = True
        case.pending_retention_action = None
        case.lifecycle = CaseLifecycle.DELETED
        case.deleted_at = now
        case.retention_due_at = None
        case.updated_at = now

    def apply_outbox_result(self, message: OutboxRecord, result: Any) -> None:
        """Persist a successfully delivered external command.

        The dispatcher calls this in the same repository transaction that marks
        the outbox message delivered. Worker calls themselves happen before the
        transaction and must therefore be idempotent.
        """

        topic = OutboxTopic(message.topic)
        if topic == OutboxTopic.TARGET_GALLERY:
            gallery = TargetGalleryResult.model_validate(result)
            command = TargetGalleryCommand.model_validate(message.payload["command"])
            if (
                gallery.command_id != command.command_id
                or gallery.case_id != command.case_id
                or gallery.case_id != message.case_id
                or gallery.model_version != command.model_version
            ):
                raise ValueError("target-gallery result does not match its command")
            case = self.repository.get(message.case_id)
            allowed = (
                {CaseLifecycle.PREPARING}
                if command.source == "reference"
                else {CaseLifecycle.SEARCHING}
            )
            current_sources = {
                item.tracklet_id for item in self._active_decisions(message.case_id)
                if item.decision == "CONFIRM"
            }
            stale_sources = (
                command.source == "rebuild"
                and set(command.tracklet_ids) != current_sources
            ) or (
                command.source == "confirmed_tracklet"
                and command.tracklet_id not in current_sources
            )
            if case.lifecycle not in allowed or stale_sources:
                self._event(
                    message.case_id,
                    "TARGET_GALLERY_IGNORED",
                    payload={"lifecycle": case.lifecycle.value, "stale_sources": stale_sources},
                )
                return
            expected = case.revision
            case.gallery_ref = gallery.gallery_ref
            case.gallery_model_version = gallery.model_version
            if command.source == "reference":
                case.lifecycle = CaseLifecycle.SEARCHING
            case.updated_at = self.clock.now()
            self.repository.save(case, expected)
            if gallery.gallery_ref.startswith(f"cases/{message.case_id}/"):
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            message.case_id,
                            "target-gallery",
                            gallery.gallery_ref,
                        ),
                        case_id=message.case_id,
                        kind="TARGET_GALLERY",
                        retention_class=RetentionClass.CASE_RESULT,
                        artifact_ref=ArtifactRef(object_key=gallery.gallery_ref),
                    )
                )
            if command.source == "reference":
                self._enqueue_routing(
                    message.case_id,
                    "GALLERY_READY",
                    f"gallery-ready:{gallery.command_id}",
                )
            self._event(
                message.case_id,
                "TARGET_GALLERY_READY",
                payload=gallery.model_dump(mode="json"),
            )
            return
        if topic == OutboxTopic.RETENTION_REGISTER:
            return
        if topic == OutboxTopic.PERCEPTION_CANCEL:
            self._event(
                message.case_id,
                "PERCEPTION_SUBSCRIPTION_CANCELLED",
                payload={"message_id": message.message_id},
            )
            return

        case = self.repository.get(message.case_id)
        expected = case.revision
        now = self.clock.now()

        if topic == OutboxTopic.CLIP_CUT:
            record = ClipRecord.model_validate(result)
            command = CutClipCommand.model_validate(message.payload["command"])
            check_cut_result(command, record)
            if (
                record.clip_request_id != command.clip_request_id
                or record.case_id != command.case_id
                or record.case_id != message.case_id
                or record.candidate_id != command.candidate_id
                or record.tracklet_id != command.tracklet_id
            ):
                raise ValueError("Clip cut result does not match its command")
            case.clips[record.clip_id] = record
            case.candidate_to_clip[record.candidate_id] = record.clip_id
            case.updated_at = now
            self.repository.save(case, expected)
            if record.artifact_ref is not None:
                self._record_case_data(
                    message.case_id,
                    CaseDataKind.ARTIFACT,
                    f"clip:{record.clip_id}:{record.revision}:{record.state}",
                    {
                        "kind": "HOLD_CLIP",
                        "clip_id": record.clip_id,
                        "artifact_ref": record.artifact_ref.model_dump(mode="json"),
                        "mime_type": record.mime_type,
                        "checksum": record.checksum,
                    },
                )
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            message.case_id,
                            record.artifact_ref.object_key,
                        ),
                        case_id=message.case_id,
                        kind="CLIP",
                        retention_class=RetentionClass.REVIEW_HOLD,
                        artifact_ref=record.artifact_ref,
                        revision=record.revision,
                    )
                )
            # Cuts can finish after route replay or revocation. The immutable
            # active decision, not a transient routing/callback flag, owns state.
            decision = next((
                item for item in self._active_decisions(message.case_id)
                if item.tracklet_id == record.tracklet_id
            ), None)
            action = (
                ("CONFIRM" if decision.decision == "CONFIRM" else "RELEASE")
                if decision is not None else None
            )
            if action is not None:
                command = ClipStateCommand(
                    command_id=self.ids.new(
                        "clip_state", message.case_id, record.tracklet_id, action
                    ),
                    case_id=message.case_id,
                    clip_id=record.clip_id,
                    action=action,
                    occurred_at=now,
                )
                self._enqueue(
                    message.case_id,
                    OutboxTopic.CLIP_STATE,
                    f"clip-state:{command.command_id}",
                    {"command": command.model_dump(mode="json")},
                )
            self._event(
                message.case_id,
                "CLIP_READY",
                payload={
                    "clip_id": record.clip_id,
                    "candidate_id": record.candidate_id,
                },
            )
            return

        if topic == OutboxTopic.CLIP_STATE:
            record = ClipRecord.model_validate(result)
            command = ClipStateCommand.model_validate(message.payload["command"])
            previous_clip = case.clips.get(record.clip_id)
            check_clip_state_result(command, record, previous=previous_clip)
            if (
                record.case_id != command.case_id
                or record.case_id != message.case_id
                or record.clip_id != command.clip_id
            ):
                raise ValueError("Clip state result does not match its command")
            case.clips[record.clip_id] = record
            if record.state == ClipLifecycleState.CONFIRMED:
                case.pending_confirm_tracklet_ids = [
                    item
                    for item in case.pending_confirm_tracklet_ids
                    if item != record.tracklet_id
                ]
            elif record.state == ClipLifecycleState.RELEASED:
                case.pending_reject_tracklet_ids = [
                    item
                    for item in case.pending_reject_tracklet_ids
                    if item != record.tracklet_id
                ]
            case.updated_at = now
            self.repository.save(case, expected)
            if record.artifact_ref is not None:
                self._record_case_data(
                    message.case_id,
                    CaseDataKind.ARTIFACT,
                    f"clip:{record.clip_id}:{record.revision}:{record.state}",
                    {
                        "kind": "CLIP",
                        "clip_id": record.clip_id,
                        "state": record.state,
                        "artifact_ref": record.artifact_ref.model_dump(mode="json"),
                        "mime_type": record.mime_type,
                        "checksum": record.checksum,
                    },
                )
                clip_retention_class = {
                    ClipLifecycleState.HOLD: RetentionClass.REVIEW_HOLD,
                    ClipLifecycleState.CONFIRMED: RetentionClass.CONFIRMED_EVIDENCE,
                    ClipLifecycleState.RELEASED: RetentionClass.EPHEMERAL,
                }[record.state]
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            message.case_id,
                            record.artifact_ref.object_key,
                        ),
                        case_id=message.case_id,
                        kind="CLIP",
                        retention_class=clip_retention_class,
                        artifact_ref=record.artifact_ref,
                        revision=record.revision,
                    )
                )
            self._event(
                message.case_id,
                "CLIP_STATE_APPLIED",
                payload={"clip_id": record.clip_id, "state": record.state},
            )
            return

        if topic == OutboxTopic.CLIP_EXPORT:
            export = ExportClipsResult.model_validate(result)
            command = ExportClipsCommand.model_validate(message.payload["command"])
            check_export_result(command, export)
            if (
                export.export_request_id != command.export_request_id
                or export.case_id != command.case_id
                or export.case_id != message.case_id
                or export.ordered_clip_ids != command.ordered_clip_ids
            ):
                raise ValueError("Clip export result does not match its command")
            if case.lifecycle in {
                CaseLifecycle.DELETE_REQUESTED,
                CaseLifecycle.DELETED,
            }:
                self._event(
                    message.case_id,
                    "CASE_EXPORT_IGNORED",
                    payload={"lifecycle": case.lifecycle.value},
                )
                return
            previous_export = (
                ExportClipsResult.model_validate(case.export_ref)
                if case.export_ref is not None
                else None
            )
            case.export_ref = export.model_dump(mode="json")
            case.lifecycle = (
                CaseLifecycle.SAVED
                if export.status == ClipProcessingStatus.READY
                else CaseLifecycle.CLOSED
            )
            case.updated_at = now
            self.repository.save(case, expected)
            if previous_export is not None and previous_export.artifact_ref is not None:
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            message.case_id,
                            previous_export.artifact_ref.object_key,
                        ),
                        case_id=message.case_id,
                        kind="SUPERSEDED_EXPORT",
                        retention_class=RetentionClass.EPHEMERAL,
                        artifact_ref=previous_export.artifact_ref,
                        revision=2,
                    )
                )
            if export.artifact_ref is not None:
                self._record_case_data(
                    message.case_id,
                    CaseDataKind.ARTIFACT,
                    f"export:{export.export_request_id}",
                    {
                        "kind": "FINAL_EXPORT",
                        "artifact_ref": export.artifact_ref.model_dump(mode="json"),
                        "checksum": export.checksum,
                        "duration_s": export.duration_s,
                    },
                )
                self._enqueue_retention_resource(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            message.case_id,
                            export.artifact_ref.object_key,
                        ),
                        case_id=message.case_id,
                        kind="EXPORT",
                        retention_class=RetentionClass.CASE_RESULT,
                        artifact_ref=export.artifact_ref,
                    )
                )
            self._event(
                message.case_id,
                (
                    "CASE_EXPORTED"
                    if export.status == ClipProcessingStatus.READY
                    else "CASE_EXPORT_FAILED"
                ),
                payload=export.model_dump(mode="json"),
            )
            return

        if topic == OutboxTopic.CLIP_PURGE:
            purge = ClipPurgeResult.model_validate(result)
            command = ClipPurgeCommand.model_validate(message.payload["command"])
            if (
                purge.command_id != command.command_id
                or purge.case_id != command.case_id
                or purge.case_id != message.case_id
                or not purge.purged
            ):
                raise ValueError("Clip purge result does not match its command")
            if case.lifecycle != CaseLifecycle.DELETE_REQUESTED:
                raise ValueError("Clip metadata purge requires DELETE_REQUESTED case")
            deletion_action = case.pending_retention_action or "DELETE_CASE"
            self._redact_deleted_case(case, now=now)
            self.repository.save(case, expected)
            self.repository.purge_case_content(
                message.case_id,
                preserve_outbox_message_id=message.message_id,
            )
            self._event(
                message.case_id,
                "CASE_DELETED" if deletion_action == "DELETE_CASE" else "CASE_TTL_DELETED",
                payload={"clip_purge_command_id": purge.command_id},
            )
            return

        if topic == OutboxTopic.RETENTION_COMMAND:
            retention = RetentionResult.model_validate(result)
            command = RetentionCommand.model_validate(message.payload["command"])
            if (
                retention.command_id != command.command_id
                or retention.case_id != command.case_id
                or retention.case_id != message.case_id
                or retention.action != command.action
            ):
                raise ValueError("Retention result does not match its command")
            if (
                retention.action in {"DELETE_CASE", "TTL_EXPIRED"}
                and retention.held_resource_ids
            ):
                case.pending_retention_action = retention.action
                case.updated_at = now
                self.repository.save(case, expected)
                self._event(
                    message.case_id,
                    "CASE_DELETE_BLOCKED_BY_LEGAL_HOLD",
                    payload={
                        "retention_command_id": retention.command_id,
                        "held_resource_ids": retention.held_resource_ids,
                    },
                )
                return
            if retention.action in {"DELETE_CASE", "TTL_EXPIRED"}:
                purge = ClipPurgeCommand(
                    command_id=self.ids.new(
                        "clip_purge", message.case_id, retention.command_id
                    ),
                    case_id=message.case_id,
                    occurred_at=now,
                )
                case.pending_retention_action = retention.action
                case.updated_at = now
                self.repository.save(case, expected)
                self._enqueue(
                    message.case_id,
                    OutboxTopic.CLIP_PURGE,
                    f"clip-purge:{purge.command_id}",
                    {"command": purge.model_dump(mode="json")},
                )
                event_type = "CASE_MEDIA_DELETED"
            elif retention.action == "CLOSE_CASE":
                event_type = "RETENTION_APPLIED"
            else:
                event_type = "RETENTION_APPLIED"
            self._event(
                message.case_id,
                event_type,
                payload=retention.model_dump(mode="json"),
            )
            return

        if topic == OutboxTopic.RETENTION_HOLD:
            hold_result = RetentionHoldResult.model_validate(result)
            hold_command = RetentionHoldCommand.model_validate(
                message.payload["command"]
            )
            if (
                hold_result.command_id != hold_command.command_id
                or hold_result.case_id != hold_command.case_id
                or hold_result.case_id != message.case_id
                or hold_result.hold_id != hold_command.hold_id
                or hold_result.action != hold_command.action
            ):
                raise ValueError(
                    "Retention legal-hold result does not match its command"
                )
            hold = case.legal_holds.get(hold_result.hold_id)
            if hold is None:
                raise ValueError("Retention returned an unknown legal hold")
            expected_active = {
                hold_id
                for hold_id, item in case.legal_holds.items()
                if item.active
                and not (
                    hold_result.action == "RELEASE" and hold_id == hold_result.hold_id
                )
            }
            if set(hold_result.active_hold_ids) != expected_active:
                raise ValueError("Retention active legal-hold projection diverged")
            if hold_result.action == "RELEASE":
                hold.active = False
                hold.release_pending = False
                case.updated_at = now
                self.repository.save(case, expected)
                if (
                    not expected_active
                    and case.lifecycle == CaseLifecycle.DELETE_REQUESTED
                    and case.pending_retention_action in {"DELETE_CASE", "TTL_EXPIRED"}
                ):
                    retry = RetentionCommand(
                        command_id=self.ids.new(
                            "retention",
                            message.case_id,
                            "hold-release",
                            hold_result.command_id,
                            case.pending_retention_action,
                        ),
                        case_id=message.case_id,
                        action=case.pending_retention_action,
                        actor_id=hold.released_by,
                        occurred_at=hold.released_at or now,
                    )
                    self._enqueue(
                        message.case_id,
                        OutboxTopic.RETENTION_COMMAND,
                        f"retention-command:{retry.command_id}",
                        {"command": retry.model_dump(mode="json")},
                    )
                event_type = "LEGAL_HOLD_RELEASED"
            else:
                event_type = "LEGAL_HOLD_APPLIED"
            self._event(
                message.case_id,
                event_type,
                actor_id=hold_command.actor_id,
                payload=hold_result.model_dump(mode="json"),
            )
            return

        raise ValueError(f"unsupported outbox topic {message.topic!r}")

    def get_case(self, case_id: str) -> CaseDetailView:
        case = self.repository.get(case_id)
        return CaseDetailView(
            case_id=case.request.case_id,
            target_id=case.request.target_id,
            profile=case.request.profile,
            lifecycle=case.lifecycle,
            revision=case.revision,
            route_revision=(
                case.latest_route.revision if case.latest_route is not None else 0
            ),
            site_id=case.request.site_id,
            site_version=case.request.site_version,
            params_version=case.params_version,
            last_known=case.request.last_known,
            window=case.request.window,
            reference_preview_url=(
                f"/v1/cases/{case_id}/reference"
                if case.lifecycle
                not in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}
                else None
            ),
            created_at=case.created_at,
            updated_at=case.updated_at,
            closed_at=case.closed_at,
            retention_due_at=case.retention_due_at,
        )

    def list_cases(
        self, *, lifecycle: CaseLifecycle | None = None
    ) -> list[CaseSummary]:
        states = {lifecycle.value} if lifecycle is not None else None
        summaries: list[CaseSummary] = []
        for case in self.repository.list_cases(lifecycles=states):
            summaries.append(
                CaseSummary(
                    case_id=case.request.case_id,
                    target_id=case.request.target_id,
                    lifecycle=case.lifecycle,
                    revision=case.revision,
                    route_revision=(
                        case.latest_route.revision if case.latest_route else 0
                    ),
                    mode=(
                        case.latest_route.step_result.mode.value
                        if case.latest_route is not None
                        else None
                    ),
                    site_id=case.request.site_id,
                    site_version=case.request.site_version,
                    created_at=case.created_at,
                    updated_at=case.updated_at,
                )
            )
        return summaries

    def get_view(self, case_id: str) -> CaseView:
        return build_case_view(
            self.repository.get(case_id),
            jobs=self.repository.list_jobs(case_id),
            outbox=self.repository.list_outbox(case_id=case_id),
            routing_triggers=self.repository.list_routing_triggers(case_id=case_id),
            probe_results=self.repository.list_probe_results(case_id),
            decisions=self._decision_history(case_id),
            now=self.clock.now(),
        )

    def get_reference_media(self, case_id: str) -> tuple[bytes, str]:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("reference image is no longer available")
        reference = case.request.reference_image_ref
        if reference is None or reference.object_key is None:
            raise KeyError("reference image is not available")
        data, mime_type = self.artifacts.get(reference.object_key)
        if mime_type != "image/jpeg":
            raise RuntimeError("stored reference image has unexpected MIME type")
        if (
            reference.sha256 is not None
            and hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise RuntimeError("stored reference image checksum mismatch")
        return data, mime_type

    def get_events(self, case_id: str) -> list[dict[str, Any]]:
        self.repository.get(case_id)
        return [
            event.model_dump(mode="json")
            for event in self.repository.list_events(case_id)
        ]

    def get_clips(self, case_id: str) -> list[dict[str, Any]]:
        return list(self.get_view(case_id).clips)

    def get_clip_media(self, case_id: str, clip_id: str) -> tuple[bytes, str]:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("clip is no longer available")
        clip = case.clips.get(clip_id)
        if clip is None or clip.status != ClipProcessingStatus.READY:
            raise KeyError("clip is not available")
        if clip.artifact_ref is None or clip.artifact_ref.object_key is None:
            raise KeyError("clip has no product artifact")
        data, mime_type = self.artifacts.get(clip.artifact_ref.object_key)
        if clip.artifact_ref.sha256 is not None and hashlib.sha256(data).hexdigest() != clip.artifact_ref.sha256:
            raise RuntimeError("stored clip checksum mismatch")
        return data, mime_type

    def get_export_media(self, case_id: str) -> tuple[bytes, str]:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("export is no longer available")
        if case.export_ref is None:
            raise KeyError("export is not available")
        export = ExportClipsResult.model_validate(case.export_ref)
        if (
            export.status != ClipProcessingStatus.READY
            or export.artifact_ref is None
            or export.artifact_ref.object_key is None
        ):
            raise KeyError("export is not available")
        data, mime_type = self.artifacts.get(export.artifact_ref.object_key)
        if export.artifact_ref.sha256 is not None and hashlib.sha256(data).hexdigest() != export.artifact_ref.sha256:
            raise RuntimeError("stored export checksum mismatch")
        return data, mime_type

    def get_candidate_thumbnail(
        self, case_id: str, candidate_id: str, index: int
    ) -> tuple[bytes, str]:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("candidate thumbnail is no longer available")
        candidate = case.candidates.get(candidate_id)
        tracklet = (
            case.tracklets.get(str(candidate.get("tracklet_id", "")))
            if candidate is not None
            else None
        )
        paths = list(tracklet.get("crop_paths") or []) if tracklet else []
        if index < 0 or index >= len(paths):
            raise KeyError("candidate thumbnail is not available")
        object_key = str(paths[index])
        if not object_key.startswith(f"cases/{case_id}/"):
            raise KeyError("candidate thumbnail is not a case artifact")
        return self.artifacts.get(object_key)

    def get_route_svg(self, case_id: str, route_revision: int, floor_id: str) -> bytes:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("route is no longer available")
        key = self._stored_route_key(case_id, route_revision, floor_id)
        data, mime_type = self.artifacts.get(key)
        if mime_type != "image/svg+xml":
            raise RuntimeError("stored route artifact has unexpected MIME type")
        return data

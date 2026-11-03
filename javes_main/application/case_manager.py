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
    ClipLifecycleState,
    ClipProcessingStatus,
    ClipRecord,
    ClipStateCommand,
    CreateCaseBody,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
    Hypothesis,
    LiveFeedUpdate,
    PerceptionProbeJob,
    ProbeResult,
    ProbeStatus,
    RetentionClass,
    RetentionCommand,
    RetentionResource,
    RetentionResult,
    ReviewDecisionBody,
    TargetGalleryCommand,
    TargetGalleryResult,
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
    OutboxRecord,
    OutboxTopic,
    ProbeResultRecord,
    RoutingTriggerRecord,
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


logger = logging.getLogger(__name__)

# Product policy: case content is retained for fourteen calendar days after the
# case is closed. Internally all timestamps are UTC, so one day is 24 hours.
DEFAULT_RETENTION_DAYS = 14
DEFAULT_RETENTION_TTL_S = DEFAULT_RETENTION_DAYS * 24 * 60 * 60


def _fingerprint(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
        clip_padding_s: float = 5.0,
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

    def _idempotent_response(
        self, key: str, fingerprint: str
    ) -> dict[str, Any] | None:
        previous = self.repository.get_idempotency(key)
        if previous is None:
            return None
        if previous[0] != fingerprint:
            raise ValueError("idempotency key reused with different payload")
        return previous[1]

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
    ) -> None:
        self.repository.add_case_data(
            CaseDataRecord(
                record_id=record_id,
                case_id=case_id,
                kind=kind,
                occurred_at=self.clock.now(),
                payload=payload,
            )
        )

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
                    raise ValueError("worker artifact MIME type does not match manifest")
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
                        artifact_ref=ArtifactRef(object_key=manifest.object_key),
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
        }:
            return current_case
        if any(
            item.record_id == trigger.trigger_id
            for item in self.repository.list_case_data(
                trigger.case_id, kinds={CaseDataKind.ROUTING_RUN.value}
            )
        ):
            return self.repository.get(trigger.case_id)
        return self._routing_tick(
            trigger.case_id,
            probe_results=trigger.probe_results,
            anchors=trigger.anchors,
            trigger_id=trigger.trigger_id,
            trigger_type=trigger.trigger_type,
        )

    def create_case(self, body: CreateCaseBody, *, idempotency_key: str) -> CaseView:
        storage_key = f"case:create:{idempotency_key}"
        fingerprint = _fingerprint(body)
        replay = self._idempotent_response(storage_key, fingerprint)
        if replay is not None:
            return self.get_view(str(replay["case_id"]))

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
        )
        state = self.routing.initialise(
            case_info,
            graph,
            params,
            Anchors(
                last_known_camera=body.last_known.camera_id,
                last_known_zone=body.last_known.zone_id,
                last_known_time=body.last_known.time,
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
        )
        replay_case_id: str | None = None
        with self.repository.transaction():
            self.repository.lock_idempotency(storage_key)
            replay = self._idempotent_response(storage_key, fingerprint)
            if replay is not None:
                replay_case_id = str(replay["case_id"])
            else:
                self.repository.add(case)
                self._event(
                    body.case_id,
                    "CASE_CREATED",
                    actor_id=body.operator_id,
                    payload={
                        "site_id": body.site_id,
                        "site_version": body.site_version,
                        "params_version": params.version,
                        "contracts_version": "javes-boundary.v1alpha1",
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
    ) -> CaseRecord:
        case = self.repository.get(case_id)
        if case.routing_state is None:
            raise RuntimeError("case has no Routing state")
        expected_revision = case.revision
        graph = self.sites.get_graph(case.request.site_id, case.request.site_version)
        params = self.sites.get_params(case.params_version)
        now = self.clock.now()
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
        case.routing_state = step.state
        route_revision = worker_result.rendered_route.route_revision
        previous_route = case.latest_route
        route_resources: list[RetentionResource] = []
        superseded_route_resources: list[RetentionResource] = []
        route_artifacts: list[dict[str, Any]] = []
        for rendered in worker_result.rendered_route.maps:
            key = (
                f"cases/{case_id}/routes/{route_revision}/"
                f"{rendered.floor_id}.svg"
            )
            svg_bytes = rendered.svg.encode("utf-8")
            self.artifacts.put(key, svg_bytes, rendered.mime_type)
            manifest = ArtifactManifest(
                object_key=key,
                kind="route_svg",
                size_bytes=len(svg_bytes),
                sha256=hashlib.sha256(svg_bytes).hexdigest(),
                created_at=now,
                retention_class="case_14d",
                mime_type=rendered.mime_type,
            )
            route_artifacts.append(
                {
                    "artifact_id": self.ids.new("artifact", key),
                    "manifest": manifest.model_dump(mode="json"),
                    "floor_id": rendered.floor_id,
                    "width": rendered.width,
                    "height": rendered.height,
                    "alt_text": rendered.alt_text,
                }
            )
            route_resources.append(
                RetentionResource(
                    resource_id=self.ids.new(
                        "resource", case_id, "route", route_revision, rendered.floor_id
                    ),
                    case_id=case_id,
                    kind="ROUTE_SVG",
                    retention_class=RetentionClass.CASE_RESULT,
                    artifact_ref=ArtifactRef(object_key=key),
                )
            )
        if previous_route is not None and previous_route.revision != route_revision:
            for rendered in previous_route.rendered.maps:
                old_key = (
                    f"cases/{case_id}/routes/{previous_route.revision}/"
                    f"{rendered.floor_id}.svg"
                )
                superseded_route_resources.append(
                    RetentionResource(
                        resource_id=self.ids.new(
                            "resource",
                            case_id,
                            "route",
                            previous_route.revision,
                            rendered.floor_id,
                        ),
                        case_id=case_id,
                        kind="SUPERSEDED_ROUTE_SVG",
                        retention_class=RetentionClass.EPHEMERAL,
                        artifact_ref=ArtifactRef(object_key=old_key),
                    )
                )
        case.latest_route = StoredRoute(
            revision=route_revision,
            step_result=step,
            rendered=worker_result.rendered_route,
            created_at=now,
        )
        case.updated_at = now
        case.next_heartbeat_at = now + timedelta(seconds=params.heartbeat_s)
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
            self._create_probe_jobs(saved, step.probes)
            for resource in route_resources:
                self._enqueue(
                    case_id,
                    OutboxTopic.RETENTION_REGISTER,
                    f"retention-register:{resource.resource_id}:{resource.retention_class.value}",
                    {"resource": resource.model_dump(mode="json")},
                )
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
                    {
                        "command": automatic_retention_command.model_dump(
                            mode="json"
                        )
                    },
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
                f"{case_id}:state:{route_revision}",
                step.state.model_dump(mode="json"),
            )
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
                        }
                        for item in worker_result.rendered_route.maps
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
        self, case: CaseRecord, probe_results: list[ProbeResult]
    ) -> list[CutClipCommand]:
        if case.latest_route is None:
            return []
        surviving = {
            tracklet_id
            for hypothesis in case.latest_route.step_result.hypotheses
            for visit in hypothesis.visits
            for tracklet_id in visit.tracklet_ids
        }
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
                commands.append(
                    CutClipCommand(
                        clip_request_id=self.ids.new(
                            "clip_req", case.request.case_id, candidate.id
                        ),
                        case_id=case.request.case_id,
                        candidate_id=candidate.id,
                        tracklet_id=tracklet.id,
                        camera_id=tracklet.camera,
                        t_from=tracklet.t_in - timedelta(seconds=self.clip_padding_s),
                        t_to=tracklet.t_out + timedelta(seconds=self.clip_padding_s),
                        padding_before_s=self.clip_padding_s,
                        padding_after_s=self.clip_padding_s,
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
            job.lease_expires_at = None if job.state != JobState.LEASED else job.lease_expires_at
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
            if job.payload.deadline is not None and job.payload.deadline <= current_time:
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
                item.payload
                for item in self.repository.list_case_data(
                    case_id, kinds={CaseDataKind.OPERATOR_DECISION.value}
                )
                if item.payload.get("tracklet_id") == body.tracklet_id
            ]
            if any(
                item.get("decision") != body.decision for item in prior_decisions
            ):
                raise ValueError(
                    "contradictory review requires the future revoke workflow"
                )
            if prior_decisions:
                self.repository.remember_idempotency(
                    storage_key, fingerprint, {"case_id": case_id}
                )
                return self.get_view(case_id)
            anchors = Anchors(
                confirm_tracklet_ids=[body.tracklet_id]
                if body.decision == "CONFIRM"
                else [],
                reject_tracklet_ids=[body.tracklet_id]
                if body.decision == "REJECT"
                else [],
            )
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
            if body.decision == "CONFIRM":
                gallery_command = TargetGalleryCommand(
                    command_id=self.ids.new(
                        "gallery_cmd", case_id, "confirmed", body.tracklet_id
                    ),
                    case_id=case_id,
                    source="confirmed_tracklet",
                    tracklet_id=body.tracklet_id,
                    model_version=(
                        case.gallery_model_version
                        or "osnet_ain_x1_0_msmt17"
                    ),
                )
                self._enqueue(
                    case_id,
                    OutboxTopic.TARGET_GALLERY,
                    f"target-gallery:{gallery_command.command_id}",
                    {"command": gallery_command.model_dump(mode="json")},
                )
            clip_id = case.candidate_to_clip.get(body.candidate_id)
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
        }
        ordered: list[str] = []
        for visit in best.visits:
            for tracklet_id in visit.tracklet_ids:
                clip = clip_by_tracklet.get(tracklet_id)
                if (
                    clip is not None
                    and clip.clip_id not in ordered
                ):
                    ordered.append(clip.clip_id)
        return ordered

    def _export(self, case_id: str, body: CaseCommandBody) -> None:
        case = self.repository.get(case_id)
        if case.lifecycle not in {CaseLifecycle.CLOSED, CaseLifecycle.SAVED}:
            raise ValueError(f"cannot export case while it is {case.lifecycle.value}")
        ordered = self._ordered_confirmed_clips(case)
        if not ordered:
            raise ValueError("case has no confirmed clips in route order")
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
        case.live_feed = None
        case.next_heartbeat_at = None
        case.updated_at = now
        self.repository.save(case, expected)
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
                if (
                    current.retention_due_at is None
                    or current.retention_due_at > now
                ):
                    continue
                expected = current.revision
                current.lifecycle = CaseLifecycle.DELETE_REQUESTED
                current.updated_at = now
                self.repository.save(current, expected)
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
                else {CaseLifecycle.SEARCHING, CaseLifecycle.CLOSED}
            )
            if case.lifecycle not in allowed:
                self._event(
                    message.case_id,
                    "TARGET_GALLERY_IGNORED",
                    payload={"lifecycle": case.lifecycle.value},
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
                resource = RetentionResource(
                    resource_id=self.ids.new(
                        "resource", message.case_id, "clip", record.clip_id
                    ),
                    case_id=message.case_id,
                    kind="HOLD_CLIP",
                    retention_class=RetentionClass.REVIEW_HOLD,
                    artifact_ref=record.artifact_ref,
                )
                self._enqueue(
                    message.case_id,
                    OutboxTopic.RETENTION_REGISTER,
                    (
                        "retention-register:"
                        f"{resource.resource_id}:{record.revision}:{record.state}"
                    ),
                    {"resource": resource.model_dump(mode="json")},
                )
            action = None
            if record.tracklet_id in case.pending_confirm_tracklet_ids:
                action = "CONFIRM"
            elif record.tracklet_id in case.pending_reject_tracklet_ids:
                action = "RELEASE"
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
                payload={"clip_id": record.clip_id, "candidate_id": record.candidate_id},
            )
            return

        if topic == OutboxTopic.CLIP_STATE:
            record = ClipRecord.model_validate(result)
            command = ClipStateCommand.model_validate(message.payload["command"])
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
                resource = RetentionResource(
                    resource_id=self.ids.new(
                        "resource", message.case_id, "clip", record.clip_id
                    ),
                    case_id=message.case_id,
                    kind=(
                        "CONFIRMED_CLIP"
                        if record.state == ClipLifecycleState.CONFIRMED
                        else "RELEASED_CLIP"
                    ),
                    retention_class=(
                        RetentionClass.CONFIRMED_EVIDENCE
                        if record.state == ClipLifecycleState.CONFIRMED
                        else RetentionClass.EPHEMERAL
                    ),
                    artifact_ref=record.artifact_ref,
                )
                self._enqueue(
                    message.case_id,
                    OutboxTopic.RETENTION_REGISTER,
                    (
                        "retention-register:"
                        f"{resource.resource_id}:{record.revision}:{record.state}"
                    ),
                    {"resource": resource.model_dump(mode="json")},
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
                            "export",
                            previous_export.export_request_id,
                        ),
                        case_id=message.case_id,
                        kind="SUPERSEDED_EXPORT",
                        retention_class=RetentionClass.EPHEMERAL,
                        artifact_ref=previous_export.artifact_ref,
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
                resource = RetentionResource(
                    resource_id=self.ids.new(
                        "resource",
                        message.case_id,
                        "export",
                        export.export_request_id,
                    ),
                    case_id=message.case_id,
                    kind="FINAL_EXPORT",
                    retention_class=RetentionClass.CASE_RESULT,
                    artifact_ref=export.artifact_ref,
                )
                self._enqueue(
                    message.case_id,
                    OutboxTopic.RETENTION_REGISTER,
                    f"retention-register:{resource.resource_id}:{resource.retention_class.value}",
                    {"resource": resource.model_dump(mode="json")},
                )
            self._event(
                message.case_id,
                "CASE_EXPORTED" if export.status == ClipProcessingStatus.READY else "CASE_EXPORT_FAILED",
                payload=export.model_dump(mode="json"),
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
            if retention.action in {"DELETE_CASE", "TTL_EXPIRED"}:
                self._redact_deleted_case(case, now=now)
                self.repository.save(case, expected)
                self.repository.purge_case_content(
                    message.case_id,
                    preserve_outbox_message_id=message.message_id,
                )
                event_type = (
                    "CASE_DELETED"
                    if retention.action == "DELETE_CASE"
                    else "CASE_TTL_DELETED"
                )
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

        raise ValueError(f"unsupported outbox topic {message.topic!r}")

    def get_case(self, case_id: str) -> dict[str, Any]:
        case = self.repository.get(case_id)
        return {
            "case_id": case.request.case_id,
            "lifecycle": case.lifecycle.value,
            "revision": case.revision,
            "route_revision": (
                case.latest_route.revision if case.latest_route is not None else 0
            ),
            "site_id": case.request.site_id,
            "site_version": case.request.site_version,
            "params_version": case.params_version,
            "created_at": case.created_at.isoformat(),
            "updated_at": case.updated_at.isoformat(),
        }

    def list_cases(self, *, lifecycle: CaseLifecycle | None = None) -> list[CaseSummary]:
        states = {lifecycle.value} if lifecycle is not None else None
        summaries: list[CaseSummary] = []
        for case in self.repository.list_cases(lifecycles=states):
            summaries.append(
                CaseSummary(
                    case_id=case.request.case_id,
                    target_id=case.request.target_id,
                    lifecycle=case.lifecycle,
                    revision=case.revision,
                    route_revision=(case.latest_route.revision if case.latest_route else 0),
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
            now=self.clock.now(),
        )

    def get_events(self, case_id: str) -> list[dict[str, Any]]:
        self.repository.get(case_id)
        return [
            event.model_dump(mode="json")
            for event in self.repository.list_events(case_id)
        ]

    def get_clips(self, case_id: str) -> list[dict[str, Any]]:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            return []
        return [
            item.model_dump(mode="json")
            for item in sorted(case.clips.values(), key=lambda value: value.clip_id)
        ]

    def get_route_svg(
        self, case_id: str, route_revision: int, floor_id: str
    ) -> bytes:
        case = self.repository.get(case_id)
        if case.lifecycle in {CaseLifecycle.DELETE_REQUESTED, CaseLifecycle.DELETED}:
            raise KeyError("route is no longer available")
        key = f"cases/{case_id}/routes/{route_revision}/{floor_id}.svg"
        data, mime_type = self.artifacts.get(key)
        if mime_type != "image/svg+xml":
            raise RuntimeError("stored route artifact has unexpected MIME type")
        return data

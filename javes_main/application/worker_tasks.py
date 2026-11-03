"""HTTP worker-task delivery over the existing jobs and outbox stores."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from javes_contracts import (
    ArtifactManifest,
    ClipRecord,
    ExportClipsResult,
    ProbeResult,
    TargetGalleryResult,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerTaskAck,
    WorkerTaskEnvelope,
    WorkerTaskFailureRequest,
    WorkerTaskResultRequest,
)
from javes_main.domain import JobState, OutboxState, OutboxTopic
from javes_main.ports import CaseRepository, Clock

from .case_manager import CaseManager, _fingerprint


COMPONENT_TOPICS: dict[str, set[str]] = {
    "perception": {
        OutboxTopic.TARGET_GALLERY.value,
        OutboxTopic.PERCEPTION_CANCEL.value,
    },
    "clip": {
        OutboxTopic.CLIP_CUT.value,
        OutboxTopic.CLIP_STATE.value,
        OutboxTopic.CLIP_EXPORT.value,
    },
    "retention": {
        OutboxTopic.RETENTION_REGISTER.value,
        OutboxTopic.RETENTION_COMMAND.value,
    },
}

TOPIC_OPERATIONS = {
    OutboxTopic.TARGET_GALLERY.value: "GALLERY_BUILD",
    OutboxTopic.PERCEPTION_CANCEL.value: "PERCEPTION_CANCEL",
    OutboxTopic.CLIP_CUT.value: "CLIP_CUT",
    OutboxTopic.CLIP_STATE.value: "CLIP_STATE",
    OutboxTopic.CLIP_EXPORT.value: "CLIP_EXPORT",
    OutboxTopic.RETENTION_REGISTER.value: "RETENTION_REGISTER",
    OutboxTopic.RETENTION_COMMAND.value: "RETENTION_DELETE",
}


class LeaseError(ValueError):
    pass


class WorkerTaskService:
    """At-least-once delivery with signed, attempt-scoped leases."""

    def __init__(
        self,
        *,
        repository: CaseRepository,
        manager: CaseManager,
        clock: Clock,
        lease_secret: str,
    ) -> None:
        if not lease_secret:
            raise ValueError("worker lease secret must not be empty")
        self.repository = repository
        self.manager = manager
        self.clock = clock
        # Hashing produces a fixed-strength HMAC key while retaining backwards
        # compatibility with short tokens in the legacy in-memory test setup.
        self._secret = hashlib.sha256(lease_secret.encode("utf-8")).digest()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise LeaseError("lease timestamp is not timezone-aware")
        return value.astimezone(timezone.utc)

    def _encode_lease(self, claims: dict[str, Any]) -> str:
        body = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(body).rstrip(b"=")
        signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return (
            encoded.decode()
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        )

    def _decode_lease(self, token: str) -> dict[str, Any]:
        try:
            encoded, supplied = token.split(".", 1)
            expected = hmac.new(
                self._secret, encoded.encode(), hashlib.sha256
            ).digest()
            supplied_bytes = base64.urlsafe_b64decode(
                supplied + "=" * (-len(supplied) % 4)
            )
            if not hmac.compare_digest(expected, supplied_bytes):
                raise LeaseError("invalid lease token signature")
            payload = base64.urlsafe_b64decode(
                encoded + "=" * (-len(encoded) % 4)
            )
            claims = json.loads(payload)
        except LeaseError:
            raise
        except Exception as exc:
            raise LeaseError("malformed lease token") from exc
        if not isinstance(claims, dict):
            raise LeaseError("malformed lease claims")
        return claims

    def _lease_token(
        self,
        *,
        component: str,
        source: str,
        task_id: str,
        attempt: int,
        worker_id: str,
        expires_at: datetime,
    ) -> str:
        return self._encode_lease(
            {
                "component": component,
                "source": source,
                "task_id": task_id,
                "attempt": attempt,
                "worker_id": worker_id,
                "expires_at": self._utc(expires_at).isoformat(),
            }
        )

    @staticmethod
    def _outbox_payload(message) -> dict[str, Any]:
        for key in ("command", "resource", "job"):
            if key in message.payload:
                return dict(message.payload[key])
        return dict(message.payload)

    def claim(self, component: str, request: WorkerClaimRequest) -> WorkerClaimResponse:
        topics = COMPONENT_TOPICS.get(component)
        if topics is None:
            raise KeyError(f"unknown worker component {component!r}")
        now = self.clock.now()
        expires = now + timedelta(seconds=request.lease_seconds)
        tasks: list[WorkerTaskEnvelope] = []
        with self.repository.transaction():
            self.repository.reclaim_expired_outbox(now=now)
            if component == "perception":
                self.repository.reclaim_expired_jobs(now=now)
            messages = self.repository.claim_outbox(
                worker_id=request.worker_id,
                now=now,
                lease_expires_at=expires,
                limit=request.limit,
                topics=topics,
            )
            for message in messages:
                tasks.append(
                    WorkerTaskEnvelope(
                        task_id=message.message_id,
                        case_id=message.case_id,
                        component=component,
                        operation=TOPIC_OPERATIONS[message.topic],
                        attempt=message.attempt,
                        lease_token=self._lease_token(
                            component=component,
                            source="outbox",
                            task_id=message.message_id,
                            attempt=message.attempt,
                            worker_id=request.worker_id,
                            expires_at=expires,
                        ),
                        lease_expires_at=expires,
                        payload=self._outbox_payload(message),
                    )
                )
            remaining = request.limit - len(tasks)
            if component == "perception" and remaining > 0:
                jobs = self.repository.claim_jobs(
                    worker_id=request.worker_id,
                    now=now,
                    lease_expires_at=expires,
                    limit=remaining,
                )
                for job in jobs:
                    tasks.append(
                        WorkerTaskEnvelope(
                            task_id=job.job_id,
                            case_id=job.case_id,
                            component="perception",
                            operation="PROBE",
                            attempt=job.attempt,
                            lease_token=self._lease_token(
                                component="perception",
                                source="job",
                                task_id=job.job_id,
                                attempt=job.attempt,
                                worker_id=request.worker_id,
                                expires_at=expires,
                            ),
                            lease_expires_at=expires,
                            deadline=job.payload.deadline,
                            payload=job.payload.model_dump(mode="json", by_alias=True),
                        )
                    )
        return WorkerClaimResponse(tasks=tasks)

    def _current(self, component: str, task_id: str, lease_token: str):
        claims = self._decode_lease(lease_token)
        if claims.get("component") != component or claims.get("task_id") != task_id:
            raise LeaseError("lease token does not match task")
        source = claims.get("source")
        if source == "job":
            record = self.repository.get_job(task_id)
        elif source == "outbox":
            record = self.repository.get_outbox(task_id)
            if record.topic not in COMPONENT_TOPICS[component]:
                raise LeaseError("task belongs to another component")
        else:
            raise LeaseError("unknown lease source")
        if record.attempt != int(claims.get("attempt", -1)):
            raise LeaseError("lease token belongs to an old delivery attempt")
        if record.lease_owner != claims.get("worker_id"):
            raise LeaseError("lease is owned by another worker")
        if record.lease_expires_at is None:
            raise LeaseError("task no longer has an active lease")
        claimed_expiry = datetime.fromisoformat(str(claims.get("expires_at")))
        if self._utc(record.lease_expires_at) != self._utc(claimed_expiry):
            raise LeaseError("lease token has been superseded")
        return source, record, claims

    def _require_active(self, record) -> None:
        now = self._utc(self.clock.now())
        if record.lease_expires_at is None or self._utc(record.lease_expires_at) <= now:
            raise LeaseError("lease has expired")

    @staticmethod
    def _owned_key(artifact_ref, *, case_id: str) -> str | None:
        if artifact_ref is None:
            return None
        if artifact_ref.object_key is None:
            raise ValueError("worker-owned artifact must use object_key")
        if not artifact_ref.object_key.startswith(f"cases/{case_id}/"):
            raise ValueError("worker-owned artifact is outside its case")
        return artifact_ref.object_key

    def _validate_result_manifests(
        self,
        component: str,
        source: str,
        record,
        request: WorkerTaskResultRequest,
    ) -> None:
        manifests = {item.object_key for item in request.artifacts}
        required: set[str] = set()
        if component == "perception" and source == "job":
            if request.result is None:
                raise ValueError("PROBE task requires a result")
            result = ProbeResult.model_validate(request.result)
            for tracklet in result.tracklets:
                required.update(tracklet.crop_paths)
                if tracklet.embedding_path:
                    required.add(tracklet.embedding_path)
        elif (
            component == "perception"
            and source == "outbox"
            and record.topic == OutboxTopic.TARGET_GALLERY.value
        ):
            if request.result is None:
                raise ValueError("GALLERY_BUILD task requires a result")
            gallery = TargetGalleryResult.model_validate(request.result)
            if not gallery.gallery_ref.startswith(f"cases/{record.case_id}/"):
                raise ValueError("gallery_ref must be a case-scoped object_key")
            required.add(gallery.gallery_ref)
        elif component == "clip" and request.result is not None:
            if record.topic in {
                OutboxTopic.CLIP_CUT.value,
                OutboxTopic.CLIP_STATE.value,
            }:
                clip = ClipRecord.model_validate(request.result)
                key = self._owned_key(clip.artifact_ref, case_id=record.case_id)
            elif record.topic == OutboxTopic.CLIP_EXPORT.value:
                export = ExportClipsResult.model_validate(request.result)
                key = self._owned_key(export.artifact_ref, case_id=record.case_id)
            else:
                key = None
            if key:
                required.add(key)
        missing = sorted(required - manifests)
        if missing:
            raise ValueError(
                "worker result omitted artifact manifest(s): " + ", ".join(missing)
            )

    def heartbeat(
        self,
        component: str,
        task_id: str,
        request: WorkerHeartbeatRequest,
    ) -> WorkerHeartbeatResponse:
        with self.repository.transaction():
            source, record, claims = self._current(
                component, task_id, request.lease_token
            )
            if source == "job" and record.state == JobState.CANCELLED:
                return WorkerHeartbeatResponse(
                    task_id=task_id, cancel_requested=True
                )
            expected_state = JobState.LEASED if source == "job" else OutboxState.LEASED
            if record.state != expected_state:
                raise LeaseError("task is no longer leased")
            self._require_active(record)
            expires = self.clock.now() + timedelta(seconds=request.extend_seconds)
            record.lease_expires_at = expires
            record.updated_at = self.clock.now()
            if source == "job":
                self.repository.save_job(record)
            else:
                self.repository.save_outbox(record)
            token = self._lease_token(
                component=component,
                source=source,
                task_id=task_id,
                attempt=record.attempt,
                worker_id=str(claims["worker_id"]),
                expires_at=expires,
            )
        return WorkerHeartbeatResponse(
            task_id=task_id,
            lease_token=token,
            lease_expires_at=expires,
        )

    def complete(
        self,
        component: str,
        task_id: str,
        request: WorkerTaskResultRequest,
    ) -> WorkerTaskAck:
        fingerprint = _fingerprint(
            {
                "result": request.result,
                "artifacts": [
                    item.model_dump(mode="json") for item in request.artifacts
                ],
            }
        )
        is_open_job = False
        if component == "perception":
            try:
                candidate_job = self.repository.get_job(task_id)
            except KeyError:
                candidate_job = None
            is_open_job = bool(
                candidate_job is not None
                and candidate_job.payload.probe.t_to is None
            )
        idempotency_key = f"worker-result:{component}:{task_id}"
        if is_open_job:
            # OPEN jobs legitimately publish multiple cumulative updates. The
            # exact same update is idempotent, while a new fingerprint advances
            # the same leased subscription.
            idempotency_key += f":{fingerprint}"
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("worker result replay has different content")
            return WorkerTaskAck(task_id=task_id, duplicate=True)
        with self.repository.transaction():
            self.repository.lock_idempotency(idempotency_key)
            previous = self.repository.get_idempotency(idempotency_key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError("worker result replay has different content")
                return WorkerTaskAck(task_id=task_id, duplicate=True)
            source, record, _ = self._current(
                component, task_id, request.lease_token
            )
            expected_state = (
                JobState.LEASED if source == "job" else OutboxState.LEASED
            )
            if record.state != expected_state:
                raise LeaseError("task is no longer leased")
            self._require_active(record)
            self._validate_result_manifests(
                component, source, record, request
            )

            if source == "job":
                if request.result is None:
                    raise ValueError("PROBE task requires a result")
                self.manager.submit_probe_result(
                    task_id, ProbeResult.model_validate(request.result)
                )
                case_id = record.case_id
            else:
                current = self.repository.get_outbox(task_id)
                self.manager.apply_outbox_result(current, request.result)
                current = self.repository.get_outbox(task_id)
                current.state = OutboxState.DELIVERED
                current.lease_owner = None
                current.lease_expires_at = None
                current.last_error = None
                current.updated_at = self.clock.now()
                self.repository.save_outbox(current)
                case_id = record.case_id

            if request.artifacts:
                self.manager.register_worker_artifacts(
                    case_id, task_id, request.artifacts
                )
            self.repository.remember_idempotency(
                idempotency_key, fingerprint, {"task_id": task_id}
            )
        return WorkerTaskAck(task_id=task_id)

    def fail(
        self,
        component: str,
        task_id: str,
        request: WorkerTaskFailureRequest,
    ) -> WorkerTaskAck:
        claims = self._decode_lease(request.lease_token)
        if claims.get("component") != component or claims.get("task_id") != task_id:
            raise LeaseError("lease token does not match task")
        idempotency_key = (
            f"worker-failure:{component}:{task_id}:{claims.get('attempt')}"
        )
        fingerprint = _fingerprint(
            {
                "error_code": request.error_code,
                "detail": request.detail,
                "retryable": request.retryable,
            }
        )
        previous = self.repository.get_idempotency(idempotency_key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("worker failure replay has different content")
            return WorkerTaskAck(
                task_id=task_id,
                duplicate=True,
                terminal=bool(previous[1]["terminal"]),
            )
        with self.repository.transaction():
            self.repository.lock_idempotency(idempotency_key)
            previous = self.repository.get_idempotency(idempotency_key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError("worker failure replay has different content")
                return WorkerTaskAck(
                    task_id=task_id,
                    duplicate=True,
                    terminal=bool(previous[1]["terminal"]),
                )
            source, record, _ = self._current(component, task_id, request.lease_token)
            self._require_active(record)
            record.lease_owner = None
            record.lease_expires_at = None
            record.updated_at = self.clock.now()
            record.last_error = request.error_code + (
                f": {request.detail}" if request.detail else ""
            )
            terminal = not request.retryable or record.attempt >= record.max_attempts
            if source == "job":
                record.state = JobState.FAILED if terminal else JobState.PENDING
                self.repository.save_job(record)
                if terminal:
                    self.manager.submit_probe_result(
                        record.job_id,
                        ProbeResult(
                            probe_id=record.payload.probe.probe_id,
                            status="FAILED",
                            coverage_ratio=0.0,
                            error_code=request.error_code,
                        ),
                    )
            else:
                record.state = OutboxState.FAILED if terminal else OutboxState.PENDING
                record.available_at = self.clock.now()
                self.repository.save_outbox(record)
            self.repository.remember_idempotency(
                idempotency_key,
                fingerprint,
                {"task_id": task_id, "terminal": terminal},
            )
        return WorkerTaskAck(task_id=task_id, terminal=terminal)

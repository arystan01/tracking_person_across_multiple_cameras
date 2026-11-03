from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from javes_contracts import (
    ArtifactManifest,
    LiveFeedUpdate,
    PerceptionProbeJob,
    ProbeResult,
    TargetGalleryCommand,
    WorkerTaskEnvelope,
    WorkerTaskResultRequest,
)
from javes_workers import HttpWorkerClient

from .state import PerceptionStateStore
from .worker import PerceptionWorker


def _task_fingerprint(task: WorkerTaskEnvelope) -> str:
    payload = json.dumps(
        {
            "component": task.component,
            "operation": task.operation,
            "payload": task.payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class PerceptionTaskRunner:
    def __init__(
        self,
        worker: PerceptionWorker,
        client: HttpWorkerClient,
        state: PerceptionStateStore,
    ) -> None:
        self.worker = worker
        self.client = client
        self.state = state
        self._active: dict[str, WorkerTaskEnvelope] = {}
        self._artifact_created_at: dict[str, datetime] = {}

    def _manifest(
        self, object_key: str, kind: str, now: datetime
    ) -> ArtifactManifest:
        data, mime_type = self.worker.artifacts.get(object_key)
        created_at = self._artifact_created_at.setdefault(object_key, now)
        return ArtifactManifest(
            object_key=object_key,
            kind=kind,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=created_at,
            retention_class="case_14d",
            mime_type=mime_type,
        )

    def _probe_artifacts(
        self, result: ProbeResult, now: datetime
    ) -> list[ArtifactManifest]:
        artifacts: dict[str, ArtifactManifest] = {}
        for tracklet in result.tracklets:
            for path in tracklet.crop_paths:
                artifacts[path] = self._manifest(path, "crop", now)
            if tracklet.embedding_path:
                artifacts[tracklet.embedding_path] = self._manifest(
                    tracklet.embedding_path, "embedding", now
                )
        return [artifacts[key] for key in sorted(artifacts)]

    def _execute(
        self, task: WorkerTaskEnvelope, now: datetime, *, cancelled: bool = False
    ) -> WorkerTaskResultRequest:
        if task.operation == "GALLERY_BUILD":
            result = self.worker.update_gallery(
                TargetGalleryCommand.model_validate(task.payload)
            )
            manifests = [self._manifest(result.gallery_ref, "gallery", now)]
            for index in range(result.source_count):
                source_key = (
                    f"cases/{result.case_id}/perception/gallery/sources/{index:04d}.npz"
                )
                manifests.append(self._manifest(source_key, "gallery", now))
            return WorkerTaskResultRequest(
                lease_token=task.lease_token,
                result=result.model_dump(mode="json"),
                artifacts=manifests,
            )
        if task.operation in {"PROBE", "PERCEPTION_CANCEL"}:
            job = PerceptionProbeJob.model_validate(task.payload)
            result = (
                self.worker.cancel(job, now)
                if cancelled or task.operation == "PERCEPTION_CANCEL"
                else self.worker.execute(job, now)
            )
            if task.operation == "PROBE" and job.probe.t_to is None:
                self._publish_live(task, result, now)
            return WorkerTaskResultRequest(
                lease_token=task.lease_token,
                result=result.model_dump(mode="json", by_alias=True),
                artifacts=self._probe_artifacts(result, now),
            )
        raise ValueError(f"unsupported Perception operation {task.operation!r}")

    def _publish_live(
        self, task: WorkerTaskEnvelope, result: ProbeResult, now: datetime
    ) -> None:
        if not result.candidates:
            return
        candidates = {item.tracklet_id: item for item in result.candidates}
        tracklet = max(
            (item for item in result.tracklets if item.id in candidates),
            key=lambda item: candidates[item.id].score,
            default=None,
        )
        if tracklet is None or not tracklet.crop_paths:
            return
        self.client.live(
            LiveFeedUpdate(
                update_id=f"live:{task.task_id}:{tracklet.id}:{tracklet.t_out.isoformat()}",
                case_id=task.case_id,
                camera_id=tracklet.camera,
                tracklet_id=tracklet.id,
                bbox=tracklet.bbox_last,
                observed_at=tracklet.t_out,
                stream_ref=tracklet.crop_paths[0],
                expires_at=now + timedelta(seconds=30),
            )
        )

    def _process_task(
        self, task: WorkerTaskEnvelope, *, lease_seconds: int
    ) -> None:
        fingerprint = _task_fingerprint(task)
        previous = self.state.task_result(task.task_id)
        try:
            heartbeat = self.client.heartbeat(
                task, extend_seconds=lease_seconds
            )
            if heartbeat.lease_token is not None:
                task = task.model_copy(
                    update={
                        "lease_token": heartbeat.lease_token,
                        "lease_expires_at": heartbeat.lease_expires_at,
                    }
                )
            if heartbeat.cancel_requested and task.operation == "PROBE":
                # Main also enqueues a durable PERCEPTION_CANCEL task. Close
                # local stream state now, but acknowledge cancellation through
                # that outbox task because the original job is already CANCELLED.
                self.worker.cancel(
                    PerceptionProbeJob.model_validate(task.payload),
                    datetime.now(timezone.utc),
                )
                self._active.pop(task.task_id, None)
                return
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError("task_id was reused with different payload")
                delivery = previous[1].model_copy(
                    update={"lease_token": task.lease_token}
                )
            else:
                delivery = self._execute(
                    task,
                    datetime.now(timezone.utc),
                )
                self.state.save_task_result(
                    task.task_id, fingerprint, delivery
                )
            self.client.complete(task, delivery)
            open_continues = (
                task.operation == "PROBE"
                and PerceptionProbeJob.model_validate(task.payload).probe.t_to is None
                and delivery.result is not None
                and delivery.result.get("status") == "DONE"
                and not delivery.result.get("stream_closed", False)
            )
            if open_continues:
                self.state.delete_task_result(task.task_id)
                self._active[task.task_id] = task
            else:
                self._active.pop(task.task_id, None)
        except Exception as exc:
            self._active.pop(task.task_id, None)
            try:
                self.client.fail(
                    task,
                    error_code=type(exc).__name__,
                    detail=str(exc),
                    retryable=True,
                )
            except Exception:
                pass

    def run_once(self, *, limit: int = 1, lease_seconds: int = 600) -> int:
        tasks = list(self._active.values())
        tasks.extend(self.client.claim(limit=limit, lease_seconds=lease_seconds))
        for task in tasks:
            self._process_task(task, lease_seconds=lease_seconds)
        return len(tasks)

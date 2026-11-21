"""Pull, renew leases, execute, persist and deliver Clip task results."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import logging
import threading

import httpx
from pydantic import ValidationError
from javes_telemetry import operation, record_counter

from javes_contracts import (
    ClipPurgeCommand,
    CutClipCommand,
    ClipStateCommand,
    ExportClipsCommand,
    WorkerTaskResultRequest,
)

from .media import MediaCancelled, MediaError
from .storage import fingerprint
from .worker import ArtifactMissingError

LOG = logging.getLogger(__name__)
MODELS = {
    'CLIP_CUT': CutClipCommand,
    'CLIP_STATE': ClipStateCommand,
    'CLIP_EXPORT': ExportClipsCommand,
    'CLIP_PURGE': ClipPurgeCommand,
}


class TaskCancelled(RuntimeError):
    pass


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, (TaskCancelled, MediaCancelled)):
        return "TASK_CANCELLED"
    if isinstance(exc, MediaError):
        return "MEDIA_FAILED"
    if isinstance(exc, ArtifactMissingError):
        return "ARTIFACT_MISSING"
    if isinstance(exc, httpx.HTTPError):
        return "SOURCE_UNAVAILABLE"
    if isinstance(exc, (ValueError, ValidationError)):
        return "SOURCE_INVALID"
    return "MEDIA_FAILED"


class ClipTaskRunner:
    def __init__(self, worker, client):
        self.worker = worker
        self.client = client

    def _execute(self, task):
        if task.component != 'clip' or task.operation not in MODELS:
            raise ValueError('unsupported Clip operation')
        command = MODELS[task.operation].model_validate(task.payload)
        if command.case_id != task.case_id:
            raise ValueError('task and command case mismatch')
        if task.deadline and task.deadline <= datetime.now(timezone.utc):
            raise ValueError('task deadline exceeded')
        digest = fingerprint({'case_id': task.case_id, 'operation': task.operation, 'payload': task.payload})
        key = 'task:'+task.task_id
        with self.worker.store.transaction():
            previous = self.worker.store.cached(key, digest)
        if previous is not None:
            return previous
        action = {
            'CLIP_CUT': self.worker.cut,
            'CLIP_STATE': self.worker.change_state,
            'CLIP_EXPORT': self.worker.export,
            'CLIP_PURGE': self.worker.purge,
        }[task.operation]
        operation_name = {
            'CLIP_CUT': 'clip.cut',
            'CLIP_STATE': 'clip.transition',
            'CLIP_EXPORT': 'clip.export',
            'CLIP_PURGE': 'clip.purge',
        }[task.operation]
        with operation(operation_name, {'component': 'clip', 'operation': task.operation}):
            result = action(command)
        response = {'result': result.model_dump(mode='json'),
                    'artifacts': [m.model_dump(mode='json') for m in self.worker.manifest(result)]}
        # Commit before HTTP callback so lost acknowledgements are replayable.
        with self.worker.store.transaction():
            self.worker.store.remember(key, digest, response, case_id=task.case_id)
        return response

    def run_once(self, *, limit=1, lease_seconds=300):
        # Process one lease at a time; never let a batch expire behind long media work.
        if limit != 1 or not 5 <= lease_seconds <= 600:
            raise ValueError('Clip worker claims one task with a 5–600 second lease')
        with operation('clip.claim', {'component': 'clip', 'operation': 'claim'}):
            tasks = self.client.claim(limit=1, lease_seconds=lease_seconds)
        record_counter('clip_tasks_claimed', len(tasks), {'component': 'clip'})
        for task in tasks:
            current = task
            try:
                state_lock = threading.Lock()
                heartbeat_error: list[BaseException] = []

                def renew():
                    nonlocal current
                    with state_lock:
                        snapshot = current
                    reply = self.client.heartbeat(snapshot, extend_seconds=lease_seconds)
                    if reply.cancel_requested:
                        self.worker.media.cancel()
                        raise TaskCancelled('task cancelled')
                    if reply.lease_token:
                        with state_lock:
                            current = current.model_copy(update={'lease_token': reply.lease_token, 'lease_expires_at': reply.lease_expires_at})

                renew()
                # Keep SQLite and media work on the owning thread. Only the HTTP
                # renewal runs in a helper thread; every replacement token is used.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    stopped = threading.Event()
                    def keep_alive():
                        try:
                            while not stopped.wait(lease_seconds/3):
                                renew()
                        except BaseException as exc:
                            heartbeat_error.append(exc)
                            self.worker.media.cancel()
                    future = pool.submit(keep_alive)
                    try:
                        response = self._execute(current)
                    finally:
                        stopped.set()
                        future.result()
                if heartbeat_error:
                    raise heartbeat_error[0]
                # A last renewal closes the race between a long operation and
                # callback, and guarantees use of the latest replacement token.
                renew()
                with state_lock:
                    callback_task = current
                with operation('clip.callback', {'component': 'clip', 'operation': 'result'}):
                    self.client.complete(callback_task, WorkerTaskResultRequest(lease_token=callback_task.lease_token, **response))
                record_counter('clip_tasks_completed', 1, {'component': 'clip', 'outcome': 'success'})
            except (TaskCancelled, MediaCancelled):
                LOG.info('Clip task %s was cancelled', task.task_id)
                record_counter('clip_tasks_completed', 1, {'component': 'clip', 'outcome': 'cancelled'})
            except Exception as exc:
                retryable = not isinstance(exc, (ValueError, ValidationError))
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 401, 403, 404, 409, 422}:
                    retryable = False
                LOG.warning('Clip task %s failed (%s)', task.task_id, type(exc).__name__)
                record_counter('clip_tasks_completed', 1, {'component': 'clip', 'outcome': 'failed'})
                try:
                    self.client.fail(
                        current,
                        error_code=_failure_code(exc),
                        detail=None,
                        retryable=retryable,
                    )
                except Exception:
                    LOG.warning('Could not deliver failure for Clip task %s; lease will expire', task.task_id)
        return len(tasks)

"""Pull, renew leases, execute, persist and deliver Clip task results."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import logging

import httpx
from pydantic import ValidationError

from javes_contracts import CutClipCommand, ClipStateCommand, ExportClipsCommand, WorkerTaskResultRequest

from .storage import fingerprint

LOG = logging.getLogger(__name__)
MODELS = {'CLIP_CUT': CutClipCommand, 'CLIP_STATE': ClipStateCommand, 'CLIP_EXPORT': ExportClipsCommand}


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
        action = {'CLIP_CUT': self.worker.cut, 'CLIP_STATE': self.worker.change_state, 'CLIP_EXPORT': self.worker.export}[task.operation]
        result = action(command)
        response = {'result': result.model_dump(mode='json'),
                    'artifacts': [m.model_dump(mode='json') for m in self.worker.manifest(result)]}
        # Commit before HTTP callback so lost acknowledgements are replayable.
        with self.worker.store.transaction():
            self.worker.store.remember(key, digest, response)
        return response

    def run_once(self, *, limit=1, lease_seconds=300):
        # Process one lease at a time; never let a batch expire behind long media work.
        if limit != 1 or not 5 <= lease_seconds <= 600:
            raise ValueError('Clip worker claims one task with a 5–600 second lease')
        tasks = self.client.claim(limit=1, lease_seconds=lease_seconds)
        for task in tasks:
            current = task
            try:
                def renew():
                    nonlocal current
                    reply = self.client.heartbeat(current, extend_seconds=lease_seconds)
                    if reply.cancel_requested:
                        raise ValueError('task cancelled')
                    if reply.lease_token:
                        current = current.model_copy(update={'lease_token': reply.lease_token, 'lease_expires_at': reply.lease_expires_at})

                renew()
                # Keep SQLite and media work on the owning thread. Only the HTTP
                # renewal runs in a helper thread; every replacement token is used.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    import threading
                    stopped = threading.Event()
                    def keep_alive():
                        while not stopped.wait(lease_seconds/3):
                            renew()
                    future = pool.submit(keep_alive)
                    try:
                        response = self._execute(current)
                    finally:
                        stopped.set()
                        future.result()
                self.client.complete(current, WorkerTaskResultRequest(lease_token=current.lease_token, **response))
            except Exception as exc:
                retryable = not isinstance(exc, (ValueError, ValidationError))
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 401, 403, 404, 409, 422}:
                    retryable = False
                LOG.warning('Clip task %s failed (%s)', task.task_id, type(exc).__name__)
                try:
                    self.client.fail(current, error_code=type(exc).__name__, detail=str(exc)[:1500], retryable=retryable)
                except Exception:
                    LOG.warning('Could not deliver failure for Clip task %s; lease will expire', task.task_id)
        return len(tasks)

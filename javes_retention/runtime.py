from __future__ import annotations

from javes_contracts import (
    RetentionCommand,
    RetentionResource,
    WorkerTaskResultRequest,
)
from javes_workers import HttpWorkerClient

from .worker import RetentionWorker


class RetentionTaskRunner:
    def __init__(self, worker: RetentionWorker, client: HttpWorkerClient) -> None:
        self.worker = worker
        self.client = client

    def run_once(self, *, limit: int = 10, lease_seconds: int = 300) -> int:
        tasks = self.client.claim(limit=limit, lease_seconds=lease_seconds)
        for task in tasks:
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
                if task.operation == "RETENTION_REGISTER":
                    self.worker.register(
                        RetentionResource.model_validate(task.payload)
                    )
                    result = None
                elif task.operation == "RETENTION_DELETE":
                    result = self.worker.process(
                        RetentionCommand.model_validate(task.payload)
                    ).model_dump(mode="json")
                else:
                    raise ValueError(
                        f"unsupported Retention operation {task.operation!r}"
                    )
                self.client.complete(
                    task,
                    WorkerTaskResultRequest(
                        lease_token=task.lease_token, result=result
                    ),
                )
            except Exception as exc:
                try:
                    self.client.fail(
                        task,
                        error_code=type(exc).__name__,
                        detail=str(exc),
                        retryable=True,
                    )
                except Exception:
                    pass
        return len(tasks)

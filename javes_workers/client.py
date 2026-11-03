from __future__ import annotations

import httpx

from javes_contracts import (
    LiveFeedUpdate,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerTaskAck,
    WorkerTaskEnvelope,
    WorkerTaskFailureRequest,
    WorkerTaskResultRequest,
)


class HttpWorkerClient:
    def __init__(
        self,
        main_url: str,
        component: str,
        token: str,
        worker_id: str,
        *,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.main_url = main_url.rstrip("/")
        self.component = component
        self.worker_id = worker_id
        self._headers = {"authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=timeout_s)

    def claim(self, *, limit: int = 1, lease_seconds: int = 30) -> list[WorkerTaskEnvelope]:
        body = WorkerClaimRequest(
            worker_id=self.worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        response = self._client.post(
            f"{self.main_url}/internal/v1/workers/{self.component}/tasks/claim",
            headers=self._headers,
            json=body.model_dump(mode="json"),
        )
        response.raise_for_status()
        return WorkerClaimResponse.model_validate(response.json()).tasks

    def heartbeat(
        self, task: WorkerTaskEnvelope, *, extend_seconds: int = 30
    ) -> WorkerHeartbeatResponse:
        body = WorkerHeartbeatRequest(
            lease_token=task.lease_token, extend_seconds=extend_seconds
        )
        response = self._client.post(
            f"{self.main_url}/internal/v1/workers/{self.component}/tasks/{task.task_id}/heartbeat",
            headers=self._headers,
            json=body.model_dump(mode="json"),
        )
        response.raise_for_status()
        return WorkerHeartbeatResponse.model_validate(response.json())

    def complete(
        self, task: WorkerTaskEnvelope, result: WorkerTaskResultRequest
    ) -> WorkerTaskAck:
        response = self._client.post(
            f"{self.main_url}/internal/v1/workers/{self.component}/tasks/{task.task_id}/result",
            headers=self._headers,
            json=result.model_dump(mode="json"),
        )
        response.raise_for_status()
        return WorkerTaskAck.model_validate(response.json())

    def fail(
        self,
        task: WorkerTaskEnvelope,
        *,
        error_code: str,
        detail: str | None = None,
        retryable: bool = True,
    ) -> WorkerTaskAck:
        body = WorkerTaskFailureRequest(
            lease_token=task.lease_token,
            error_code=error_code,
            detail=detail,
            retryable=retryable,
        )
        response = self._client.post(
            f"{self.main_url}/internal/v1/workers/{self.component}/tasks/{task.task_id}/failure",
            headers=self._headers,
            json=body.model_dump(mode="json"),
        )
        response.raise_for_status()
        return WorkerTaskAck.model_validate(response.json())

    def live(self, update: LiveFeedUpdate) -> None:
        response = self._client.post(
            f"{self.main_url}/internal/v1/cases/{update.case_id}/live",
            headers=self._headers,
            json=update.model_dump(mode="json"),
        )
        response.raise_for_status()

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from javes_contracts import (
    RetentionClass,
    RetentionResource,
    WorkerClaimRequest,
    WorkerHeartbeatRequest,
)
from javes_main.application import WorkerTaskService
from javes_main.api import create_app
from javes_main.domain import OutboxRecord, OutboxTopic
from javes_security import RotationConfigurationError, RotationWindow
from javes_site_config.api import create_site_config_app
from tests.test_controls_api import make_system


NOW = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_rotation_window_is_bounded_and_exposes_only_fingerprints() -> None:
    rotation = RotationWindow(
        current="current-value",
        previous="previous-value",
        previous_valid_until=NOW + timedelta(minutes=5),
    )
    assert rotation.accepted(now=NOW) == ("current-value", "previous-value")
    status = rotation.status(now=NOW)
    assert "current-value" not in str(status)
    assert "previous-value" not in str(status)
    assert status["previous_accepted"] is True
    with pytest.raises(RotationConfigurationError, match="expired"):
        rotation.require_ready(now=NOW + timedelta(minutes=6))
    with pytest.raises(RotationConfigurationError, match="must differ"):
        RotationWindow(
            current="same",
            previous="same",
            previous_valid_until=NOW + timedelta(minutes=1),
        ).validate()


def test_inflight_lease_signed_by_previous_secret_survives_rotation() -> None:
    system = make_system()
    repository = system.manager.repository
    resource = RetentionResource(
        resource_id="resource-rotation",
        case_id="api-case",
        kind="TEST",
        retention_class=RetentionClass.AUDIT,
    )
    repository.add_outbox(
        OutboxRecord(
            message_id="rotation-task",
            case_id="api-case",
            topic=OutboxTopic.RETENTION_REGISTER,
            dedupe_key="rotation-task",
            payload={"resource": resource.model_dump(mode="json")},
            available_at=system.clock.now(),
            created_at=system.clock.now(),
            updated_at=system.clock.now(),
        )
    )
    old = WorkerTaskService(
        repository=repository,
        manager=system.manager,
        clock=system.clock,
        lease_secret="old-lease-secret",
    )
    task = old.claim(
        "retention",
        WorkerClaimRequest(worker_id="retention-1", limit=1, lease_seconds=30),
    ).tasks[0]
    rotated = WorkerTaskService(
        repository=repository,
        manager=system.manager,
        clock=system.clock,
        lease_secret="new-lease-secret",
        lease_secret_previous="old-lease-secret",
        lease_secret_previous_valid_until=system.clock.now() + timedelta(minutes=1),
    )
    heartbeat = rotated.heartbeat(
        "retention",
        task.task_id,
        WorkerHeartbeatRequest(lease_token=task.lease_token, extend_seconds=30),
    )
    assert heartbeat.lease_token != task.lease_token
    retired = WorkerTaskService(
        repository=repository,
        manager=system.manager,
        clock=system.clock,
        lease_secret="new-lease-secret",
    )
    with pytest.raises(ValueError, match="signature"):
        retired.heartbeat(
            "retention",
            task.task_id,
            WorkerHeartbeatRequest(lease_token=task.lease_token, extend_seconds=30),
        )


@pytest.mark.anyio
async def test_service_api_accepts_current_and_previous_only_during_overlap() -> None:
    class Registry:
        def site(self, site_id: str, version: str):
            return {"site_id": site_id, "version": version}

    rotation = RotationWindow(
        current="current-service-token",
        previous="previous-service-token",
        previous_valid_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    app = create_site_config_app(Registry(), service_token=rotation)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://site.test"
    ) as client:
        for token in ("current-service-token", "previous-service-token"):
            response = await client.get(
                "/internal/v1/sites/nvidia/versions/v1",
                headers={"authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
        denied = await client.get(
            "/internal/v1/sites/nvidia/versions/v1",
            headers={"authorization": "Bearer retired-service-token"},
        )
        assert denied.status_code == 401


@pytest.mark.anyio
async def test_main_worker_api_stops_accepting_previous_token_after_deadline() -> None:
    system = make_system()
    expired = RotationWindow(
        current="current-worker-token",
        previous="previous-worker-token",
        previous_valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    app = create_app(
        system.manager,
        auth=system.auth,
        worker_tokens={"perception": expired},
        rotation_status={"perception_worker": expired.status()},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://main.test"
    ) as client:
        denied = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers={"authorization": "Bearer previous-worker-token"},
            json={"worker_id": "perception-1"},
        )
        assert denied.status_code == 401
        accepted = await client.post(
            "/internal/v1/workers/perception/tasks/claim",
            headers={"authorization": "Bearer current-worker-token"},
            json={"worker_id": "perception-1"},
        )
        assert accepted.status_code == 200
        readiness = await client.get("/health/ready")
        assert readiness.status_code == 503
        assert "rotation overlap expired" in readiness.text

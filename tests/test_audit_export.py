from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from javes_contracts import AuditExportRequest
from javes_main.adapters import InMemoryArtifactStore
from javes_main.adapters.system import ContentIdGenerator, FixedClock
from javes_main.application import AuditExportService
from javes_main.domain import AuditEvent, ROLE_PERMISSIONS, UserRole
from javes_main.persistence.memory import InMemoryRepository
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from javes_main.api import create_app
from tests.test_controls_api import login, make_system


NOW = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_audit_export_is_paginated_deterministic_redacted_and_registered() -> None:
    repository = InMemoryRepository()
    artifacts = InMemoryArtifactStore()
    registered = []
    for index in range(53):
        repository.append_event(
            AuditEvent(
                event_id=f"event-{index:03d}",
                case_id="__iam__" if index % 5 == 0 else "case-001",
                event_type="LOGIN" if index % 2 == 0 else "CASE_UPDATED",
                actor_id="root-001",
                occurred_at=NOW + timedelta(seconds=index),
                payload={
                    "safe": index,
                    "authorization": "Bearer must-not-export",
                    "object_key": "cases/case-001/private.jpg",
                },
            )
        )
    service = AuditExportService(
        repository=repository,
        artifacts=artifacts,
        clock=FixedClock(NOW + timedelta(minutes=2)),
        ids=ContentIdGenerator(),
        register_artifact=lambda *args: registered.append(args),
    )
    request = AuditExportRequest(export_id="export-001", page_size=7)
    first = service.create(request, actor_id="root-001")
    second = service.create(request, actor_id="root-001")
    assert first == second
    assert first.row_count == 53
    events, mime_type = service.read("export-001", "events")
    assert mime_type == "application/x-ndjson"
    assert hashlib.sha256(events).hexdigest() == first.sha256
    assert len(events) == first.size_bytes
    lines = [json.loads(line) for line in events.splitlines()]
    assert lines[0]["source"] == "IAM"
    assert lines[1]["source"] == "CASE"
    assert all(item["schema_version"] == "javes-audit-event.v1" for item in lines)
    assert b"must-not-export" not in events
    assert b"private.jpg" not in events
    assert b"[REDACTED]" in events
    assert len(registered) == 2
    assert {item[2] for item in registered} == {"AUDIT_NDJSON", "AUDIT_MANIFEST"}
    exported = repository.list_events("__iam__")[-1]
    assert exported.event_type == "AUDIT_EXPORTED"
    assert "object_key" not in exported.payload
    with pytest.raises(ValueError, match="different filters"):
        service.create(
            request.model_copy(update={"case_ids": ["different"]}),
            actor_id="root-001",
        )


def test_audit_export_permission_is_root_only_by_default() -> None:
    assert "*" in ROLE_PERMISSIONS[UserRole.ROOT]
    assert "audit.export" not in ROLE_PERMISSIONS[UserRole.ADMIN]
    assert "audit.export" not in ROLE_PERMISSIONS[UserRole.OPERATOR]
    assert "audit.export" not in ROLE_PERMISSIONS[UserRole.VIEWER]


def test_audit_export_rejects_header_unsafe_identifier() -> None:
    with pytest.raises(ValueError):
        AuditExportRequest(export_id="unsafe\nidentifier")


def test_sql_audit_cursor_filters_by_actor_and_event_type() -> None:
    repository = SqlAlchemyRepository.from_url("sqlite+pysqlite:///:memory:")
    repository.create_schema()
    for index, actor in enumerate(("one", "two", "one")):
        repository.append_event(
            AuditEvent(
                event_id=f"sql-{index}",
                case_id="case-sql",
                event_type="MATCH" if index != 1 else "OTHER",
                actor_id=actor,
                occurred_at=NOW + timedelta(seconds=index),
            )
        )
    events = repository.list_audit_events(
        actor_ids={"one"}, event_types={"MATCH"}, limit=1
    )
    assert [item.event_id for item in events] == ["sql-0"]
    events = repository.list_audit_events(
        after=(events[-1].occurred_at, events[-1].event_id),
        actor_ids={"one"},
        event_types={"MATCH"},
        limit=10,
    )
    assert [item.event_id for item in events] == ["sql-2"]


@pytest.mark.anyio
async def test_audit_export_controls_api_requires_root_and_streams_bytes() -> None:
    system = make_system()
    app = create_app(system.manager, auth=system.auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://main.test"
    ) as client:
        denied = await client.post(
            "/v1/audit/exports", json={"export_id": "api-export"}
        )
        assert denied.status_code == 401
        token = await login(client, "root", "root-password-for-tests")
        created = await client.post(
            "/v1/audit/exports",
            headers={"authorization": f"Bearer {token}"},
            json={"export_id": "api-export", "page_size": 2},
        )
        assert created.status_code == 201, created.text
        manifest = created.json()
        downloaded = await client.get(
            manifest["events_url"],
            headers={"authorization": f"Bearer {token}"},
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("application/x-ndjson")
        assert hashlib.sha256(downloaded.content).hexdigest() == manifest["sha256"]

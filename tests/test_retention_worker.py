from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from javes_contracts import ArtifactRef, RetentionClass, RetentionCommand, RetentionResource
from javes_contracts import CaseCommandBody, ReviewDecisionBody
from javes_main.domain import CaseLifecycle
from javes_main.adapters import InMemoryArtifactStore, InMemoryLiveFeedStore
from javes_retention import (
    ArtifactStoreDeletionBackend,
    CallbackDeletionBackend,
    DeletionReceipt,
    DeletionRouter,
    RetentionDeletionError,
    RetentionWorker,
    SQLiteRetentionRepository,
    StorageLocator,
    build_local_retention_worker,
)
from javes_retention import LocalFileDeletionBackend


BASE = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


def command(command_id: str, action: str, *, at: datetime = BASE) -> RetentionCommand:
    return RetentionCommand(
        command_id=command_id,
        case_id="case-1",
        action=action,
        actor_id="operator-1",
        occurred_at=at,
    )


def resource(
    resource_id: str,
    retention_class: RetentionClass,
    *,
    key: str | None = None,
    expires_at: datetime | None = None,
    legal_hold: bool = False,
    artifact_ref: ArtifactRef | None = None,
) -> RetentionResource:
    return RetentionResource(
        resource_id=resource_id,
        case_id="case-1",
        kind=resource_id.upper(),
        retention_class=retention_class,
        artifact_ref=artifact_ref or (ArtifactRef(object_key=key) if key else None),
        expires_at=expires_at,
        legal_hold=legal_hold,
    )


def worker(
    repository: SQLiteRetentionRepository,
    store: InMemoryArtifactStore,
    *,
    extra: dict[str, object] | None = None,
) -> RetentionWorker:
    router = DeletionRouter(
        {"object_key": ArtifactStoreDeletionBackend(store), **(extra or {})}
    )
    return RetentionWorker(repository, router, clock=lambda: BASE)


def test_close_delete_policy_physically_removes_bytes_and_keeps_audit(tmp_path):
    store = InMemoryArtifactStore()
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, store)
    values = [
        resource("crop", RetentionClass.EPHEMERAL, key="crop.jpg"),
        resource("hold", RetentionClass.REVIEW_HOLD, key="hold.mp4"),
        resource("route", RetentionClass.CASE_RESULT, key="route.svg"),
        resource("confirmed", RetentionClass.CONFIRMED_EVIDENCE, key="clip.mp4"),
        resource("audit", RetentionClass.AUDIT),
        resource(
            "legal",
            RetentionClass.CONFIRMED_EVIDENCE,
            key="legal.mp4",
            legal_hold=True,
        ),
    ]
    for item in values:
        if item.artifact_ref and item.artifact_ref.object_key:
            store.put(item.artifact_ref.object_key, b"payload", "application/octet-stream")
        retention.register(item)

    closed = retention.process(command("close-1", "CLOSE_CASE"))
    assert closed.deleted_resource_ids == ["crop", "hold"]
    assert closed.preserved_resource_ids == ["audit", "confirmed", "legal", "route"]
    assert store.keys() == ["clip.mp4", "legal.mp4", "route.svg"]

    deleted = retention.process(command("delete-1", "DELETE_CASE", at=BASE + timedelta(seconds=1)))
    assert deleted.deleted_resource_ids == ["confirmed", "route"]
    assert deleted.preserved_resource_ids == ["audit", "legal"]
    assert store.keys() == ["legal.mp4"]
    tombstone = retention.get_tombstone(deleted.audit_tombstone_id)
    assert tombstone is not None
    assert tombstone["policy_version"] == "retention-policy.v1"
    assert tombstone["deleted_count"] == 2
    stored_command = repo.get_command("delete-1")
    assert stored_command is not None
    assert stored_command.command.actor_id == "operator-1"
    health = retention.health_snapshot()
    assert health["legal_holds"] == 1
    assert health["failed_deletions"] == 0
    assert health["tombstones"] == 2


def test_ttl_deletes_due_and_inherited_but_preserves_future_audit_and_hold(tmp_path):
    store = InMemoryArtifactStore()
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, store)
    for item in [
        resource("inherited", RetentionClass.CASE_RESULT),
        resource("due", RetentionClass.CONFIRMED_EVIDENCE, expires_at=BASE),
        resource(
            "future",
            RetentionClass.CONFIRMED_EVIDENCE,
            expires_at=BASE + timedelta(days=1),
        ),
        resource("audit", RetentionClass.AUDIT),
        resource("held", RetentionClass.CASE_RESULT, legal_hold=True),
    ]:
        retention.register(item)

    result = retention.process(command("ttl-1", "TTL_EXPIRED"))
    assert result.deleted_resource_ids == ["due", "inherited"]
    assert result.preserved_resource_ids == ["audit", "future", "held"]
    assert retention.sweep_expired(BASE + timedelta(hours=1)) == []
    swept = retention.sweep_expired(BASE + timedelta(days=2))
    assert len(swept) == 1
    assert swept[0].deleted_resource_ids == ["future"]
    assert retention.sweep_expired(BASE + timedelta(days=2)) == []


def test_late_resource_and_released_legal_hold_cannot_resurrect_deleted_case(tmp_path):
    store = InMemoryArtifactStore()
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, store)
    held = resource(
        "held", RetentionClass.CONFIRMED_EVIDENCE, key="held.mp4", legal_hold=True
    )
    store.put("held.mp4", b"held", "video/mp4")
    retention.register(held)
    result = retention.process(command("delete-1", "DELETE_CASE"))
    assert result.preserved_resource_ids == ["held"]

    late = resource("late", RetentionClass.CASE_RESULT, key="late.svg")
    store.put("late.svg", b"late", "image/svg+xml")
    retention.register(late)
    late_stored = retention.get_resource("late")
    assert late_stored is not None and late_stored.state == "DELETED"
    assert late_stored.resource.artifact_ref is None
    assert "late.svg" not in store.keys()

    retention.register(held.model_copy(update={"legal_hold": False}))
    assert retention.get_resource("held").state == "DELETED"  # type: ignore[union-attr]
    assert store.keys() == []


def test_already_missing_object_is_a_proven_idempotent_delete(tmp_path):
    store = InMemoryArtifactStore()
    retention = worker(SQLiteRetentionRepository(tmp_path / "retention.db"), store)
    retention.register(resource("missing", RetentionClass.EPHEMERAL, key="absent"))
    result = retention.process(command("close-missing", "CLOSE_CASE"))
    assert result.deleted_resource_ids == ["missing"]
    stored = retention.get_resource("missing")
    assert stored is not None
    assert stored.resource.artifact_ref is None
    assert stored.resource.kind == "DELETED"
    assert stored.proof[0]["already_absent"] is True
    assert stored.proof[0]["locator"]["value"].startswith("sha256:")


def test_stale_registration_cannot_resurrect_terminal_resource_class(tmp_path):
    store = InMemoryArtifactStore()
    retention = worker(SQLiteRetentionRepository(tmp_path / "retention.db"), store)
    final = resource("route", RetentionClass.CASE_RESULT, key="route.svg")
    retention.register(final)
    retention.register(
        final.model_copy(
            update={
                "kind": "SUPERSEDED_ROUTE_SVG",
                "retention_class": RetentionClass.EPHEMERAL,
            }
        )
    )
    # Simulate the original CASE_RESULT outbox delivery arriving late.
    retention.register(final)
    stored = retention.get_resource("route")
    assert stored is not None
    assert stored.resource.retention_class == RetentionClass.EPHEMERAL
    assert stored.resource.kind == "SUPERSEDED_ROUTE_SVG"


def test_partial_failure_is_durable_and_resumes_after_restart(tmp_path):
    database = tmp_path / "retention.db"
    calls: list[str] = []

    def fail_second(locator: StorageLocator) -> bool:
        calls.append(locator.value)
        if locator.value == "b" and calls.count("b") == 1:
            raise RuntimeError("temporary artifact-storage outage")
        return True

    repo = SQLiteRetentionRepository(database)
    first = RetentionWorker(
        repo,
        DeletionRouter(
            {"object_key": CallbackDeletionBackend(fail_second, name="flaky")}
        ),
        clock=lambda: BASE,
    )
    first.register(resource("a", RetentionClass.EPHEMERAL, key="a"))
    first.register(resource("b", RetentionClass.EPHEMERAL, key="b"))
    with pytest.raises(RetentionDeletionError, match="resource 'b'"):
        first.process(command("close-1", "CLOSE_CASE"))
    assert first.get_resource("a").state == "DELETED"  # type: ignore[union-attr]
    assert first.get_resource("b").state == "DELETE_FAILED"  # type: ignore[union-attr]
    repo.close()

    restarted_repo = SQLiteRetentionRepository(database)
    restarted = RetentionWorker(
        restarted_repo,
        DeletionRouter(
            {"object_key": CallbackDeletionBackend(fail_second, name="flaky")}
        ),
        clock=lambda: BASE + timedelta(seconds=1),
    )
    result = restarted.process(command("close-1", "CLOSE_CASE"))
    assert result.deleted_resource_ids == ["a", "b"]
    assert calls == ["a", "b", "b"]
    assert restarted.process(command("close-1", "CLOSE_CASE")) == result


def test_unknown_storage_scheme_fails_closed_until_backend_is_attached(tmp_path):
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    router = DeletionRouter()
    retention = RetentionWorker(repo, router, clock=lambda: BASE)
    retention.register(
        resource(
            "gallery",
            RetentionClass.EPHEMERAL,
            artifact_ref=ArtifactRef(uri="gallery:case-1/target"),
        )
    )
    with pytest.raises(RetentionDeletionError, match="uri:gallery"):
        retention.process(command("close-gallery", "CLOSE_CASE"))
    assert retention.get_resource("gallery").state == "DELETE_FAILED"  # type: ignore[union-attr]

    router.add_backend(
        "uri:gallery",
        CallbackDeletionBackend(lambda _: True, name="perception-gallery"),
    )
    result = retention.process(command("close-gallery", "CLOSE_CASE"))
    assert result.deleted_resource_ids == ["gallery"]


def test_command_conflict_resource_conflict_and_concurrent_duplicate(tmp_path):
    store = InMemoryArtifactStore()
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, store)
    retention.register(resource("one", RetentionClass.EPHEMERAL))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: retention.process(command("close-1", "CLOSE_CASE")),
                range(4),
            )
        )
    assert all(item == results[0] for item in results)

    with pytest.raises(ValueError, match="different payload"):
        retention.process(command("close-1", "DELETE_CASE"))
    with pytest.raises(ValueError, match="move between cases"):
        retention.register(
            resource("one", RetentionClass.EPHEMERAL).model_copy(
                update={"case_id": "case-2"}
            )
        )


def test_duplicate_command_across_two_worker_instances_is_safe(tmp_path):
    database = tmp_path / "retention.db"
    store = InMemoryArtifactStore()
    store.put("shared", b"x", "application/octet-stream")
    first = worker(SQLiteRetentionRepository(database), store)
    second = worker(SQLiteRetentionRepository(database), store)
    first.register(resource("shared", RetentionClass.EPHEMERAL, key="shared"))
    close = command("cross-process-close", "CLOSE_CASE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: item.process(close), [first, second]))
    assert results[0] == results[1]
    assert store.keys() == []


def test_external_archive_reference_cannot_be_retention_owned(tmp_path):
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, InMemoryArtifactStore())
    with pytest.raises(ValueError, match="external read-only CCTV"):
        retention.register(
            resource(
                "external-source",
                RetentionClass.EPHEMERAL,
                artifact_ref=ArtifactRef(archive_id="archive-1"),
            )
        )


def test_local_file_backend_is_allowlisted_non_recursive_and_idempotent(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    artifact = allowed / "crop.jpg"
    artifact.write_bytes(b"crop")
    backend = LocalFileDeletionBackend([allowed])
    locator = StorageLocator("uri:file", artifact.as_uri())
    assert backend.delete(locator).deleted is True
    assert backend.delete(locator).already_absent is True

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do not delete")
    with pytest.raises(ValueError, match="outside"):
        backend.delete(StorageLocator("uri:file", outside.as_uri()))
    assert outside.exists()
    directory = allowed / "directory"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        backend.delete(StorageLocator("uri:file", directory.as_uri()))


def test_local_factory_deletes_live_buffer_and_artifact_files(tmp_path):
    artifacts = InMemoryArtifactStore()
    live = InMemoryLiveFeedStore()
    artifacts.put("crop", b"crop", "image/jpeg")
    live.put("case/camera/frame", b"frame")
    retention = build_local_retention_worker(
        database_path=tmp_path / "retention.db",
        artifact_store=artifacts,
        live_feed_store=live,
        clock=lambda: BASE,
    )
    retention.register(resource("crop", RetentionClass.EPHEMERAL, key="crop"))
    retention.register(
        resource(
            "live",
            RetentionClass.EPHEMERAL,
            artifact_ref=ArtifactRef(uri="live:case/camera/frame"),
        )
    )
    result = retention.process(command("close-local", "CLOSE_CASE"))
    assert result.deleted_resource_ids == ["crop", "live"]
    assert artifacts.keys() == []
    with pytest.raises(KeyError):
        live.get("case/camera/frame")


def test_real_retention_plugs_into_main_and_deletes_route_bytes(tmp_path):
    from tests.test_main_infra import BASE as MAIN_BASE
    from tests.test_main_infra import create_body, system

    app = system()
    gallery_deletes: list[str] = []

    def delete_gallery(locator: StorageLocator) -> bool:
        gallery_deletes.append(locator.value)
        return True

    repository = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = RetentionWorker(
        repository,
        DeletionRouter(
            {
                "object_key": ArtifactStoreDeletionBackend(app.artifacts),
                "uri:gallery": CallbackDeletionBackend(
                    delete_gallery, name="fake-gallery"
                ),
            }
        ),
        clock=app.clock.now,
    )
    app.outbox_dispatcher.retention = retention
    app.manager.retention = retention

    app.manager.create_case(create_body(), idempotency_key="real-retention-create")
    app.scheduler.run_case_once("case-1")
    view = app.manager.get_view("case-1")
    assert [key for key in app.artifacts.keys() if "/routes/" in key]
    candidate = view.candidates[0]
    app.manager.decide(
        "case-1",
        ReviewDecisionBody(
            decision_id="confirm-real-retention",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id="operator-1",
            occurred_at=MAIN_BASE + timedelta(seconds=70),
        ),
        idempotency_key="confirm-real-retention",
    )
    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()
    preclose_view = app.manager.get_view("case-1")
    route_keys = [key for key in app.artifacts.keys() if "/routes/" in key]
    latest_route_keys = [
        key
        for key in route_keys
        if f"/routes/{preclose_view.route_revision}/" in key
    ]
    superseded_route_keys = sorted(set(route_keys) - set(latest_route_keys))
    assert latest_route_keys and superseded_route_keys
    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="close-real-retention",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=MAIN_BASE + timedelta(seconds=80),
        ),
        idempotency_key="close-real-retention",
    )
    app.outbox_dispatcher.drain()
    closed = app.repository.get("case-1")
    assert closed.tracklets == {}
    assert closed.candidates == {}
    assert list(closed.clips.values())[0].state == "CONFIRMED"
    sanitized_jobs = app.repository.list_jobs("case-1")
    assert sanitized_jobs
    assert all(
        job.payload.target_gallery_ref == "__deleted__"
        and job.payload.probe.camera == "__deleted__"
        for job in sanitized_jobs
    )
    sanitized_results = app.repository.list_probe_results("case-1")
    assert sanitized_results
    assert all(
        not record.result.tracklets and not record.result.candidates
        for record in sanitized_results
    )
    temporary_kinds = {
        "ROUTING_STATE",
        "ROUTING_RUN",
        "PROBE_REQUEST",
        "TRACKLET",
        "CANDIDATE",
    }
    assert all(
        record.payload == {"redacted": True}
        for record in app.repository.list_case_data("case-1")
        if record.kind.value in temporary_kinds
    )
    assert any(
        event.payload != {"redacted": True}
        for event in app.repository.list_events("case-1")
    )
    assert len(gallery_deletes) == 1
    assert gallery_deletes[0].startswith("gallery:")
    assert all(key in app.artifacts.keys() for key in latest_route_keys)
    assert all(key not in app.artifacts.keys() for key in superseded_route_keys)

    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="delete-real-retention",
            action="DELETE",
            operator_id="root",
            occurred_at=MAIN_BASE + timedelta(seconds=90),
        ),
        idempotency_key="delete-real-retention",
    )
    app.outbox_dispatcher.drain()
    deleted = app.repository.get("case-1")
    assert deleted.lifecycle == CaseLifecycle.DELETED
    assert deleted.content_redacted is True
    assert all(key not in app.artifacts.keys() for key in route_keys)


def test_main_scheduler_auto_deletes_case_at_retention_deadline(tmp_path):
    from tests.test_main_infra import BASE as MAIN_BASE
    from tests.test_main_infra import create_body, system

    app = system()
    app.manager.retention_ttl_s = 10
    repository = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = RetentionWorker(
        repository,
        DeletionRouter(
            {
                "object_key": ArtifactStoreDeletionBackend(app.artifacts),
                "uri:gallery": CallbackDeletionBackend(
                    lambda _: False, name="fake-gallery"
                ),
            }
        ),
        clock=app.clock.now,
    )
    app.outbox_dispatcher.retention = retention
    app.manager.retention = retention

    app.manager.create_case(create_body(), idempotency_key="ttl-create")
    app.scheduler.run_case_once("case-1")
    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="ttl-close",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=MAIN_BASE + timedelta(seconds=70),
        ),
        idempotency_key="ttl-close",
    )
    app.outbox_dispatcher.drain()
    due = app.repository.get("case-1").retention_due_at
    assert due is not None
    app.clock.set(due + timedelta(seconds=1))

    report = app.scheduler.run_once()
    assert report.retention_expiries == 1
    assert app.repository.get("case-1").lifecycle == CaseLifecycle.DELETED
    assert app.scheduler.run_once().retention_expiries == 0


def test_main_default_retention_deadline_is_fourteen_calendar_days():
    from tests.test_main_infra import create_body, system

    app = system()
    app.manager.create_case(create_body(), idempotency_key="default-ttl-create")
    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="default-ttl-close",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=70),
        ),
        idempotency_key="default-ttl-close",
    )

    closed = app.repository.get("case-1")
    assert closed.closed_at is not None
    assert closed.retention_due_at == closed.closed_at + timedelta(days=14)

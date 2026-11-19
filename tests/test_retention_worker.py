from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from javes_contracts import (
    ArtifactRef,
    RetentionClass,
    RetentionCommand,
    RetentionHoldCommand,
    RetentionResource,
)
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


def hold_command(
    command_id: str,
    hold_id: str,
    action: str,
    *,
    ticket: str = "SEC-123",
) -> RetentionHoldCommand:
    return RetentionHoldCommand(
        command_id=command_id,
        case_id="case-1",
        hold_id=hold_id,
        action=action,
        external_ticket_id=ticket,
        reason=f"{action.lower()} requested by legal",
        actor_id="root-1",
        occurred_at=BASE,
    )


def test_case_legal_holds_protect_existing_and_future_resources_until_last_release(
    tmp_path,
):
    store = InMemoryArtifactStore()
    repo = SQLiteRetentionRepository(tmp_path / "retention.db")
    retention = worker(repo, store)
    for item in [
        resource("route", RetentionClass.CASE_RESULT, key="route.svg"),
        resource("audit", RetentionClass.AUDIT),
    ]:
        if item.artifact_ref and item.artifact_ref.object_key:
            store.put(item.artifact_ref.object_key, b"payload", "application/octet-stream")
        retention.register(item)

    first = retention.apply_hold(hold_command("set-1", "hold-1", "SET"))
    second = retention.apply_hold(hold_command("set-2", "hold-2", "SET", ticket="SEC-456"))
    assert first.active_hold_ids == ["hold-1"]
    assert second.active_hold_ids == ["hold-1", "hold-2"]
    blocked = retention.process(command("delete-held", "DELETE_CASE"))
    assert blocked.deleted_resource_ids == []
    assert blocked.held_resource_ids == ["route"]
    assert blocked.preserved_resource_ids == ["audit", "route"]

    late = resource("late", RetentionClass.CASE_RESULT, key="late.svg")
    store.put("late.svg", b"late", "image/svg+xml")
    retention.register(late)
    assert retention.get_resource("late").state == "ACTIVE"  # type: ignore[union-attr]

    released_one = retention.apply_hold(
        hold_command("release-1", "hold-1", "RELEASE")
    )
    assert released_one.active_hold_ids == ["hold-2"]
    still_blocked = retention.process(command("delete-still-held", "DELETE_CASE"))
    assert still_blocked.held_resource_ids == ["late", "route"]

    released_last = retention.apply_hold(
        hold_command("release-2", "hold-2", "RELEASE", ticket="SEC-456")
    )
    assert released_last.active_hold_ids == []
    deleted = retention.process(command("delete-after-release", "DELETE_CASE"))
    assert deleted.deleted_resource_ids == ["late", "route"]
    assert deleted.preserved_resource_ids == ["audit"]
    assert deleted.held_resource_ids == []
    assert store.keys() == []


def test_legal_hold_commands_are_durable_idempotent_and_fingerprint_checked(tmp_path):
    path = tmp_path / "retention.db"
    store = InMemoryArtifactStore()
    first = worker(SQLiteRetentionRepository(path), store)
    command_value = hold_command("set-durable", "hold-durable", "SET")
    expected = first.apply_hold(command_value)
    first.close()

    restarted = worker(SQLiteRetentionRepository(path), store)
    assert restarted.apply_hold(command_value) == expected
    with pytest.raises(ValueError, match="different payload"):
        restarted.apply_hold(
            command_value.model_copy(update={"reason": "different reason"})
        )
    with pytest.raises(KeyError, match="unknown legal hold"):
        restarted.apply_hold(hold_command("release-missing", "missing", "RELEASE"))


def test_close_retains_until_delete_and_keeps_audit(tmp_path):
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
    assert closed.deleted_resource_ids == []
    assert closed.preserved_resource_ids == [
        "audit", "confirmed", "crop", "hold", "legal", "route"
    ]
    assert store.keys() == [
        "clip.mp4", "crop.jpg", "hold.mp4", "legal.mp4", "route.svg"
    ]

    deleted = retention.process(command("delete-1", "DELETE_CASE", at=BASE + timedelta(seconds=1)))
    assert deleted.deleted_resource_ids == ["confirmed", "crop", "hold", "route"]
    assert deleted.preserved_resource_ids == ["audit", "legal"]
    assert store.keys() == ["legal.mp4"]
    tombstone = retention.get_tombstone(deleted.audit_tombstone_id)
    assert tombstone is not None
    assert tombstone["policy_version"] == "retention-policy.v1"
    assert tombstone["deleted_count"] == 4
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


def test_late_resource_and_unauthorised_hold_release_cannot_resurrect_case(tmp_path):
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

    with pytest.raises(ValueError, match="authority workflow"):
        retention.register(held.model_copy(update={"legal_hold": False}))
    assert retention.get_resource("held").state == "ACTIVE"  # type: ignore[union-attr]
    assert store.keys() == ["held.mp4"]


def test_already_missing_object_is_a_proven_idempotent_delete(tmp_path):
    store = InMemoryArtifactStore()
    retention = worker(SQLiteRetentionRepository(tmp_path / "retention.db"), store)
    retention.register(resource("missing", RetentionClass.EPHEMERAL, key="absent"))
    result = retention.process(command("ttl-missing", "TTL_EXPIRED"))
    assert result.deleted_resource_ids == ["missing"]
    stored = retention.get_resource("missing")
    assert stored is not None
    assert stored.resource.artifact_ref is None
    assert stored.resource.kind == "DELETED"
    assert stored.proof[0]["status"] == "ALREADY_MISSING"
    assert stored.proof[0]["locator_hash"].startswith("sha256:")
    assert "absent" not in str(stored.proof)
    assert result.receipts[0].status == "ALREADY_MISSING"


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
        first.process(command("ttl-1", "TTL_EXPIRED"))
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
    result = restarted.process(command("ttl-1", "TTL_EXPIRED"))
    assert result.deleted_resource_ids == ["a", "b"]
    assert calls == ["a", "b", "b"]
    b_receipts = [item for item in result.receipts if item.resource_id == "b"]
    assert [(item.attempt, item.status) for item in b_receipts] == [
        (1, "FAILED"),
        (2, "DELETED"),
    ]
    assert all(item.locator_hash.startswith("sha256:") for item in b_receipts)
    assert restarted.process(command("ttl-1", "TTL_EXPIRED")) == result


def test_crash_after_journal_resumes_without_losing_command(tmp_path):
    database = tmp_path / "retention.db"
    store = InMemoryArtifactStore()
    store.put("journalled", b"payload", "application/octet-stream")
    first = worker(SQLiteRetentionRepository(database), store)
    first.register(
        resource("journalled", RetentionClass.EPHEMERAL, key="journalled")
    )

    def crash_before_delete(_: str):
        raise SystemExit("simulated process crash after command journal")

    first._delete_resource = crash_before_delete  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        first.process(command("crash-after-journal", "TTL_EXPIRED"))
    stored_command = first.repository.get_command("crash-after-journal")
    assert stored_command is not None and stored_command.state == "PROCESSING"
    first.close()

    restarted = worker(SQLiteRetentionRepository(database), store)
    result = restarted.process(command("crash-after-journal", "TTL_EXPIRED"))
    assert result.deleted_resource_ids == ["journalled"]
    assert store.keys() == []


def test_delete_before_receipt_retries_as_already_missing(tmp_path):
    database = tmp_path / "retention.db"
    store = InMemoryArtifactStore()
    store.put("crash-window", b"payload", "application/octet-stream")

    class CrashAfterDeleteBackend:
        name = "crash-after-delete"

        def __init__(self) -> None:
            self.calls = 0

        def delete(self, locator: StorageLocator) -> DeletionReceipt:
            self.calls += 1
            existed = store.delete(locator.value)
            if self.calls == 1:
                raise RuntimeError("simulated crash before backend receipt")
            return DeletionReceipt(
                locator=locator,
                deleted=existed,
                already_absent=not existed,
                backend=self.name,
            )

    backend = CrashAfterDeleteBackend()
    first = RetentionWorker(
        SQLiteRetentionRepository(database),
        DeletionRouter({"object_key": backend}),
        clock=lambda: BASE,
    )
    first.register(
        resource("crash-window", RetentionClass.EPHEMERAL, key="crash-window")
    )
    with pytest.raises(RetentionDeletionError, match="RUNTIME_ERROR"):
        first.process(command("crash-after-delete", "TTL_EXPIRED"))
    failed = first.get_resource("crash-window")
    assert failed is not None
    assert failed.proof[0]["status"] == "FAILED"
    assert "locator" not in failed.proof[0]
    assert failed.proof[0]["locator_hash"].startswith("sha256:")
    first.close()

    restarted = RetentionWorker(
        SQLiteRetentionRepository(database),
        DeletionRouter({"object_key": backend}),
        clock=lambda: BASE + timedelta(seconds=1),
    )
    result = restarted.process(command("crash-after-delete", "TTL_EXPIRED"))
    assert [(item.attempt, item.status) for item in result.receipts] == [
        (1, "FAILED"),
        (2, "ALREADY_MISSING"),
    ]
    assert backend.calls == 2


def test_callback_ack_crash_replays_cached_result_without_second_delete(tmp_path):
    calls = 0

    def delete_once(_: StorageLocator) -> bool:
        nonlocal calls
        calls += 1
        return True

    retention = RetentionWorker(
        SQLiteRetentionRepository(tmp_path / "retention.db"),
        DeletionRouter(
            {"object_key": CallbackDeletionBackend(delete_once, name="counted")}
        ),
        clock=lambda: BASE,
    )
    retention.register(
        resource("callback", RetentionClass.EPHEMERAL, key="callback")
    )
    command_value = command("callback-before-ack", "TTL_EXPIRED")
    result_before_ack = retention.process(command_value)
    # Main ACK is deliberately absent here: at-least-once transport redelivers.
    replayed = retention.process(command_value)
    assert replayed == result_before_ack
    assert calls == 1


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
    with pytest.raises(RetentionDeletionError, match="UNSUPPORTED_STORAGE_LOCATOR"):
        retention.process(command("ttl-gallery", "TTL_EXPIRED"))
    assert retention.get_resource("gallery").state == "DELETE_FAILED"  # type: ignore[union-attr]

    router.add_backend(
        "uri:gallery",
        CallbackDeletionBackend(lambda _: True, name="perception-gallery"),
    )
    result = retention.process(command("ttl-gallery", "TTL_EXPIRED"))
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
    close = command("cross-process-ttl", "TTL_EXPIRED")
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
    result = retention.process(command("ttl-local", "TTL_EXPIRED"))
    assert result.deleted_resource_ids == ["crop", "live"]
    assert artifacts.keys() == []
    with pytest.raises(KeyError):
        live.get("case/camera/frame")


def test_all_current_product_artifact_kinds_delete_with_path_free_receipts(tmp_path):
    store = InMemoryArtifactStore()
    retention = worker(SQLiteRetentionRepository(tmp_path / "retention.db"), store)
    resources = {
        "reference": "cases/case-1/reference/reference.jpg",
        "crop": "cases/case-1/perception/crop.jpg",
        "embedding": "cases/case-1/perception/embedding.npy",
        "gallery": "cases/case-1/perception/gallery.npy",
        "live_jpeg": "cases/case-1/live/frame.jpg",
        "route_svg": "cases/case-1/routes/1/warehouse.svg",
    }
    for kind, key in resources.items():
        store.put(key, kind.encode(), "application/octet-stream")
        retention.register(
            RetentionResource(
                resource_id=kind,
                case_id="case-1",
                kind=kind.upper(),
                retention_class=RetentionClass.CASE_RESULT,
                artifact_ref=ArtifactRef(object_key=key),
            )
        )
    retention.register(resource("audit", RetentionClass.AUDIT))

    result = retention.process(command("delete-artifact-matrix", "DELETE_CASE"))
    assert result.deleted_resource_ids == sorted(resources)
    assert result.preserved_resource_ids == ["audit"]
    assert store.keys() == []
    assert len(result.receipts) == len(resources)
    assert {item.status for item in result.receipts} == {"DELETED"}
    serialized = result.model_dump_json()
    assert not any(key in serialized for key in resources.values())


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
    assert closed.tracklets
    assert closed.candidates
    assert list(closed.clips.values())[0].state == "CONFIRMED"
    sanitized_jobs = app.repository.list_jobs("case-1")
    assert sanitized_jobs
    assert all(
        job.payload.target_gallery_ref != "__deleted__"
        and job.payload.probe.camera != "__deleted__"
        for job in sanitized_jobs
    )
    sanitized_results = app.repository.list_probe_results("case-1")
    assert sanitized_results
    assert any(
        record.result.tracklets or record.result.candidates
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
        record.payload != {"redacted": True}
        for record in app.repository.list_case_data("case-1")
        if record.kind.value in temporary_kinds
    )
    assert any(
        event.payload != {"redacted": True}
        for event in app.repository.list_events("case-1")
    )
    # This isolated Main test double has no gallery bytes. Production
    # Perception uses shared-store object_key manifests.
    assert gallery_deletes == []
    assert all(key in app.artifacts.keys() for key in latest_route_keys)
    assert all(key in app.artifacts.keys() for key in superseded_route_keys)

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

"""Publication must tolerate a DB rollback *after* actual filesystem writes."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from javes_contracts import Anchors, RetentionCommand, RetentionResource
from javes_main.adapters import FileArtifactStore, InMemoryLiveFeedStore
from javes_main.domain import RoutingTriggerRecord
from javes_main.persistence import InMemoryRepository, RevisionConflict
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from javes_retention import build_local_retention_worker
from tests import test_main_infra as fixtures
from tests.support.main_system import assemble_system


@pytest.fixture(params=["memory", "sqlite"])
def app(request, tmp_path):
    template = fixtures.system()
    repository = (
        InMemoryRepository() if request.param == "memory"
        else SqlAlchemyRepository.from_url(f"sqlite:///{tmp_path / 'main.sqlite'}")
    )
    if isinstance(repository, SqlAlchemyRepository):
        repository.create_schema()
    return assemble_system(
        repository=repository, artifacts=FileArtifactStore(tmp_path / "artifacts"),
        live_feed=InMemoryLiveFeedStore(), sites=template.sites,
        perception=template.manager.perception, clip=template.manager.clip,
        retention=template.manager.retention, clock=template.clock,
    )


@pytest.mark.parametrize("operation", ["tick", "replay"])
def test_rollback_after_svg_write_recovers_after_restart(app, tmp_path, monkeypatch, operation):
    app.manager.create_case(fixtures.create_body(), idempotency_key="publication-create")
    app.scheduler.run_case_once("case-1")
    before = app.manager.get_view("case-1")
    trigger = RoutingTriggerRecord(
        trigger_id="publication-replay", case_id="case-1", dedupe_key="publication-replay",
        trigger_type="OPERATOR_DECISION", created_at=app.clock.now(), updated_at=app.clock.now(),
    )

    def advance():
        return (app.manager._replay_case(trigger) if operation == "replay" else
                app.manager._routing_tick("case-1", probe_results=[], anchors=Anchors()))

    def fail_after_write(*args, **kwargs):
        raise RuntimeError("simulated DB failure after actual SVG publication")

    with monkeypatch.context() as patch:
        patch.setattr(app.manager, "_create_probe_jobs", fail_after_write)
        with pytest.raises(RuntimeError, match="after actual SVG"):
            advance()
    assert app.manager.get_view("case-1").route_revision == before.route_revision
    intents = app.repository.list_case_data("case-1", kinds={"ARTIFACT_INTENT"})
    orphan_intents = [item for item in intents if item.payload["route_revision"] > before.route_revision]
    assert orphan_intents
    for item in orphan_intents:
        assert app.artifacts.get(item.payload["manifest"]["object_key"])[0]
    with pytest.raises(KeyError, match="not committed"):
        app.manager.get_route_svg("case-1", before.route_revision + 1, "1F")

    # A new process/store reuses durable intents, not an in-memory rollback hook.
    if isinstance(app.repository, SqlAlchemyRepository):
        repository = SqlAlchemyRepository.from_url(str(app.repository.engine.url))
        app.repository = repository
        app.manager.repository = repository
    app.manager.artifacts = FileArtifactStore(tmp_path / "artifacts")
    app.clock.set(app.clock.now() + timedelta(seconds=1))
    advance()
    after = app.manager.get_view("case-1")
    assert after.route_revision > before.route_revision
    assert app.manager.get_route_svg("case-1", after.route_revision, "1F")
    assert app.manager.get_route_svg("case-1", before.route_revision, "1F")

    # All published attempts, including the uncommitted one, are reclaimable.
    retention = build_local_retention_worker(
        database_path=tmp_path / "retention.sqlite", artifact_store=app.manager.artifacts,
        clock=app.clock.now,
    )
    try:
        for message in app.repository.list_outbox(case_id="case-1"):
            raw = message.payload.get("resource", {})
            if message.topic == "RETENTION_REGISTER" and raw.get("kind") in {"ROUTE_SVG", "SUPERSEDED_ROUTE_SVG"}:
                retention.register(RetentionResource.model_validate(raw))
        result = retention.process(RetentionCommand(
            command_id="delete-publication-test", case_id="case-1", action="DELETE_CASE",
            actor_id="root", occurred_at=app.clock.now(),
        ))
        assert result.deleted_resource_ids
        assert not result.preserved_resource_ids
        for item in app.repository.list_case_data("case-1", kinds={"ARTIFACT_INTENT"}):
            with pytest.raises(KeyError):
                app.manager.artifacts.get(item.payload["manifest"]["object_key"])
    finally:
        retention.close()


def test_changed_attempt_content_cannot_poison_public_revision(app):
    app.manager.create_case(fixtures.create_body(), idempotency_key="hash-create")
    app.scheduler.run_case_once("case-1")
    revision = app.manager.get_view("case-1").route_revision + 1
    first, second = [SimpleNamespace(
        floor_id="1F", width=800, height=500, alt_text="Unit publication attempt",
        mime_type="image/svg+xml", svg=svg,
    ) for svg in ("<svg>attempt-one</svg>", "<svg>attempt-two</svg>")]
    one = app.manager._prepare_route_artifacts("case-1", revision, [first])[0]
    two = app.manager._prepare_route_artifacts("case-1", revision, [second])[0]
    assert one["manifest"]["object_key"] != two["manifest"]["object_key"]
    for item, record in [(first, one), (second, two)]:
        app.artifacts.put(record["manifest"]["object_key"], item.svg.encode(), item.mime_type)
    with pytest.raises(KeyError, match="not committed"):
        app.manager.get_route_svg("case-1", revision, "1F")


def test_probe_reissue_preserves_original_deadline_gallery_and_terminal_job(app):
    app.manager.create_case(fixtures.create_body(), idempotency_key="reuse-create")
    app.scheduler.run_case_once("case-1")
    original = app.repository.list_jobs("case-1")[0]
    assert original.state.value == "DONE"
    case = app.repository.get("case-1")
    case.gallery_ref = "new-gallery-after-confirm"
    app.clock.set(app.clock.now() + timedelta(seconds=10))
    changed = original.payload.probe.model_copy(update={"priority": 0.125, "hypothesis_ids": ["new-beam"]})
    app.manager._create_probe_jobs(case, [changed])
    assert app.repository.list_jobs("case-1") == [original]
    with pytest.raises(ValueError, match="different physical request"):
        app.manager._create_probe_jobs(case, [changed.model_copy(update={"camera": "A"})])


def test_orphan_legacy_file_is_not_public(app):
    app.manager.create_case(fixtures.create_body(), idempotency_key="legacy-orphan")
    app.artifacts.put("cases/case-1/routes/999/1F.svg", b"<svg>uncommitted</svg>", "image/svg+xml")
    with pytest.raises(KeyError, match="not committed"):
        app.manager.get_route_svg("case-1", 999, "1F")


def test_deleted_case_cannot_reserve_new_route_artifacts(app):
    from javes_main.domain import CaseLifecycle

    app.manager.create_case(fixtures.create_body(), idempotency_key="deleted-reservation")
    case = app.repository.get("case-1")
    expected = case.revision
    case.lifecycle = CaseLifecycle.DELETE_REQUESTED
    app.repository.save(case, expected)
    with pytest.raises(RevisionConflict, match="no longer accepts"):
        app.manager._prepare_route_artifacts("case-1", 1, [])
    assert app.repository.list_case_data("case-1", kinds={"ARTIFACT_INTENT"}) == []


def test_exhausted_routing_is_blocked_until_explicit_recovery(app, monkeypatch):
    from javes_main.application.dlq import DeadLetterService

    app.manager.create_case(fixtures.create_body(), idempotency_key="blocked-create")
    app.scheduler.run_case_once("case-1")
    app.manager.heartbeat("case-1")
    trigger = next(t for t in app.repository.list_routing_triggers(case_id="case-1") if t.state.value == "PENDING")
    trigger.max_attempts = 1
    app.repository.save_routing_trigger(trigger)

    def fail(_):
        raise RuntimeError("private filesystem /srv/private/artifacts is unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app.manager, "process_routing_trigger", fail)
        assert app.routing_dispatcher.run_once().failed == 1
    view = app.manager.get_view("case-1")
    assert view.lifecycle.value == "SEARCHING"
    assert view.search_progress["processing_status"] == "BLOCKED"
    assert view.search_progress["active_video_jobs"] == 0
    assert "/srv/private" not in str(view.errors)
    assert app.routing_dispatcher.run_once().claimed == 0
    DeadLetterService(app.repository, app.clock).replay("routing", trigger.trigger_id)
    assert app.manager.get_view("case-1").search_progress["processing_status"] == "RETRYING"
    assert app.routing_dispatcher.run_once().completed == 1
    assert app.manager.get_view("case-1").search_progress["processing_status"] != "BLOCKED"

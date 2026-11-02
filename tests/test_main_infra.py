from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from javes_contracts import (
    ArtifactRef,
    CaseCommandBody,
    CaseWindow,
    CreateCaseBody,
    FakeObservation,
    LastKnown,
    ProbeResult,
    ProbeStatus,
    ReviewDecisionBody,
)
from javes_fakes import FakeClipWorker, FakePerceptionWorker, FakeRetentionWorker
from javes_main import assemble_system, build_in_memory_system
from javes_main.application import MetricsService
from javes_main.adapters import (
    FakeClipAdapter,
    FakePerceptionAdapter,
    FakeRetentionAdapter,
    FileArtifactStore,
    InMemoryLiveFeedStore,
    InMemorySiteConfig,
)
from javes_main.adapters.system import FixedClock
from javes_main.domain import CaseLifecycle, JobState, ProbeResultRecord
from javes_main.persistence import RevisionConflict
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from routing.contracts import CameraDef, EdgeDef, MapDef, MapFloor, SiteGraph
from routing.contracts.params import RoutingParams


UTC = timezone.utc
BASE = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def graph() -> SiteGraph:
    return SiteGraph(
        site_id="site-1",
        version="v1",
        cameras={
            "A": CameraDef(id="A", zone="west", floor="1F", map_xy=(100, 200)),
            "B": CameraDef(id="B", zone="east", floor="1F", map_xy=(500, 200)),
        },
        edges=[
            EdgeDef(
                id="A-B",
                source="A",
                target="B",
                t_min=10,
                t_typ=20,
                t_max=60,
                map_path=[(100, 200), (300, 200), (500, 200)],
            )
        ],
        map=MapDef(floors=[MapFloor(id="1F", size_px=(800, 500))]),
    )


def create_body() -> CreateCaseBody:
    return CreateCaseBody(
        case_id="case-1",
        operator_id="operator-1",
        target_id="target-1",
        reference_image_ref=ArtifactRef(object_key="cases/case-1/reference.jpg"),
        last_known=LastKnown(camera_id="A", time=BASE),
        window=CaseWindow(start=BASE, end=BASE + timedelta(minutes=5)),
        site_id="site-1",
        site_version="v1",
    )


def system():
    return build_in_memory_system(
        graph=graph(),
        params=RoutingParams(K=4),
        now=BASE + timedelta(seconds=60),
        observations=[
            FakeObservation(
                observation_id="obs-B",
                camera_id="B",
                t_start=BASE + timedelta(seconds=20),
                t_end=BASE + timedelta(seconds=30),
                appearance_score=0.91,
                bbox_first=(10, 10, 20, 40),
                bbox_last=(50, 10, 20, 40),
                clip_ref=ArtifactRef(archive_id="archive-B"),
            )
        ],
    )


def test_case_manager_end_to_end_with_all_fakes():
    app = system()
    initial = app.manager.create_case(create_body(), idempotency_key="create-1")
    assert initial.lifecycle == CaseLifecycle.PREPARING
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    jobs = app.repository.list_jobs("case-1", states={JobState.PENDING.value})
    assert [job.payload.probe.camera for job in jobs] == ["B"]

    assert app.manager.run_pending_jobs("case-1") == [jobs[0].job_id]
    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()
    view = app.manager.get_view("case-1")
    assert view.best_hypothesis is not None
    assert [visit["camera_id"] for visit in view.best_hypothesis["visits"]] == ["A", "B"]
    assert len(view.rendered_route_maps) == 1
    assert view.rendered_route_maps[0].floor_id == "1F"
    svg = app.manager.get_route_svg("case-1", view.route_revision, "1F")
    assert b'data-role="best-route"' in svg
    assert len(view.candidates) == 1
    assert len(view.clips) == 1
    assert view.clips[0]["state"] == "HOLD"
    assert view.candidates[0]["camera_id"] == "B"
    assert view.candidates[0]["clip_id"] == view.clips[0]["clip_id"]
    assert view.candidates[0]["review_state"] == "HOLD"

    candidate = view.candidates[0]
    confirmed = app.manager.decide(
        "case-1",
        ReviewDecisionBody(
            decision_id="decision-1",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="CONFIRM",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=70),
        ),
        idempotency_key="decision-1",
    )
    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()
    confirmed = app.manager.get_view("case-1")
    assert confirmed.clips[0]["state"] == "CONFIRMED"
    with pytest.raises(ValueError, match="revoke"):
        app.manager.decide(
            "case-1",
            ReviewDecisionBody(
                decision_id="contradict-confirm",
                candidate_id=candidate["candidate_id"],
                tracklet_id=candidate["tracklet_id"],
                decision="REJECT",
                operator_id="operator-1",
                occurred_at=BASE + timedelta(seconds=72),
            ),
            idempotency_key="contradict-confirm",
        )

    with pytest.raises(ValueError, match="cannot export"):
        app.manager.command(
            "case-1",
            CaseCommandBody(
                command_id="premature-export",
                action="EXPORT",
                operator_id="operator-1",
                occurred_at=BASE + timedelta(seconds=75),
            ),
            idempotency_key="premature-export",
        )

    closed = app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="close-1",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=80),
        ),
        idempotency_key="close-1",
    )
    assert closed.lifecycle == CaseLifecycle.CLOSED
    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()

    exported = app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="export-1",
            action="EXPORT",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=90),
        ),
        idempotency_key="export-1",
    )
    assert exported.lifecycle == CaseLifecycle.EXPORTING
    app.outbox_dispatcher.drain()
    exported = app.manager.get_view("case-1")
    assert exported.lifecycle == CaseLifecycle.SAVED
    assert app.repository.get("case-1").export_ref["status"] == "READY"

    still_saved = app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="close-again",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=100),
        ),
        idempotency_key="close-again",
    )
    assert still_saved.lifecycle == CaseLifecycle.SAVED

    kinds = {item.kind.value for item in app.repository.list_case_data("case-1")}
    assert {
        "ROUTING_STATE",
        "ROUTING_RUN",
        "ROUTE_REVISION",
        "PROBE_REQUEST",
        "TRACKLET",
        "CANDIDATE",
        "OPERATOR_DECISION",
        "ARTIFACT",
    } <= kinds

    deleted = app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="delete-1",
            action="DELETE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=110),
        ),
        idempotency_key="delete-1",
    )
    assert deleted.lifecycle == CaseLifecycle.DELETE_REQUESTED
    app.outbox_dispatcher.drain()
    deleted = app.manager.get_view("case-1")
    assert deleted.lifecycle == CaseLifecycle.DELETED
    events = app.manager.get_events("case-1")
    assert any(event["event_type"] == "CASE_DELETED" for event in events)
    decided_event = next(
        event for event in events if event["event_type"] == "CANDIDATE_DECIDED"
    )
    assert decided_event["actor_id"] == "operator-1"
    assert decided_event["payload"]["decision"] == "CONFIRM"
    assert decided_event["payload"]["candidate_id"] == candidate["candidate_id"]
    assert decided_event["payload"]["tracklet_id"] == candidate["tracklet_id"]
    redacted = app.repository.get("case-1")
    assert redacted.content_redacted is True
    assert redacted.request.target_id == "__deleted__"
    assert redacted.routing_state is None
    assert app.repository.list_case_data("case-1") == []
    assert app.repository.list_probe_results("case-1") == []


def test_duplicate_worker_result_and_api_request_are_idempotent():
    app = system()
    first = app.manager.create_case(create_body(), idempotency_key="create-1")
    replay = app.manager.create_case(create_body(), idempotency_key="create-1")
    assert replay.revision == first.revision
    assert len([event for event in app.manager.get_events("case-1") if event["event_type"] == "CASE_CREATED"]) == 1

    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    job = app.repository.list_jobs("case-1", states={JobState.PENDING.value})[0]
    result = app.perception_worker.process_probe(job.payload, now=app.clock.now())
    app.manager.submit_probe_result(job.job_id, result)
    app.routing_dispatcher.drain()
    revision = app.manager.get_view("case-1").revision
    replay_view = app.manager.submit_probe_result(job.job_id, result)
    assert replay_view.revision == revision
    assert len(app.repository.list_probe_results("case-1")) == 1

    changed = create_body().model_copy(update={"target_id": "different"})
    with pytest.raises(ValueError, match="idempotency"):
        app.manager.create_case(changed, idempotency_key="create-1")


def test_uncorrelated_perception_evidence_is_persisted_raw_but_not_routed():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="bad-evidence-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    job = app.repository.list_jobs("case-1", states={JobState.PENDING.value})[0]
    result = app.perception_worker.process_probe(job.payload, now=app.clock.now())
    bad_tracklet = result.tracklets[0].model_copy(update={"camera": "UNKNOWN"})
    malformed = result.model_copy(update={"tracklets": [bad_tracklet]})
    app.manager.submit_probe_result(job.job_id, malformed)
    app.routing_dispatcher.drain()

    raw = app.repository.list_probe_results("case-1")[0].result
    assert raw.tracklets[0].camera == "UNKNOWN"
    case = app.repository.get("case-1")
    assert bad_tracklet.id not in case.tracklets
    assert case.candidates == {}


def test_reject_before_clip_cut_finishes_is_applied_after_hold_arrives():
    """An operator decision must not race and lose the asynchronous Clip cut."""

    app = system()
    app.manager.create_case(create_body(), idempotency_key="early-reject-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    app.manager.run_pending_jobs("case-1")
    app.routing_dispatcher.drain()

    # Routing has accepted the candidate and queued CLIP_CUT, but the Clip
    # worker has not returned the HOLD record yet.
    view = app.manager.get_view("case-1")
    assert len(view.candidates) == 1
    assert view.clips == []
    candidate = view.candidates[0]
    app.manager.decide(
        "case-1",
        ReviewDecisionBody(
            decision_id="early-reject",
            candidate_id=candidate["candidate_id"],
            tracklet_id=candidate["tracklet_id"],
            decision="REJECT",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=65),
        ),
        idempotency_key="early-reject",
    )

    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()
    released = app.manager.get_view("case-1")
    # Routing finished the search, so CLOSE_CASE cleanup removes the released
    # temporary clip immediately after the delayed cut/state sequence settles.
    assert released.clips == []
    assert app.repository.get("case-1").pending_reject_tracklet_ids == []


def test_review_hold_is_removed_when_case_closes():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="post-close-review-create")
    app.scheduler.run_case_once("case-1")
    view = app.manager.get_view("case-1")
    clip_id = view.clips[0]["clip_id"]
    retention_resource_id = next(
        resource_id
        for resource_id, resource in app.retention_worker.resources.items()
        if resource.kind == "HOLD_CLIP"
        and resource.artifact_ref is not None
        and clip_id in resource.artifact_ref.identity()
    )

    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="post-close",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=70),
        ),
        idempotency_key="post-close",
    )
    app.routing_dispatcher.drain()
    app.outbox_dispatcher.drain()
    assert app.retention_worker.resources[retention_resource_id].deleted is True
    assert app.manager.get_view("case-1").clips == []


def test_repository_optimistic_revision_check():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="create-1")
    stale = app.repository.get("case-1")
    current = app.repository.get("case-1")
    app.repository.save(current, current.revision)
    with pytest.raises(RevisionConflict):
        app.repository.save(stale, stale.revision)


def test_case_keeps_pinned_routing_params_after_site_config_advances():
    app = system()
    original = app.sites.get_params()
    app.manager.create_case(create_body(), idempotency_key="pinned-params-create")
    app.sites.add_params(
        original.model_copy(update={"version": "next", "K": 1}),
        make_current=True,
    )
    assert app.repository.get("case-1").params_version == original.version
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    assert app.manager.get_view("case-1").route_revision == 1
    with pytest.raises(ValueError, match="immutable"):
        app.sites.add_params(original.model_copy(update={"K": original.K + 1}))
    with pytest.raises(ValueError, match="assets.*immutable"):
        app.sites.add(graph(), floor_assets={"1F": b"changed-plan"})


def test_processed_camera_seconds_use_cumulative_probe_maximum():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="camera-seconds-create")
    for fingerprint, duration in (("result-a", 10.0), ("result-b", 25.0)):
        assert app.repository.add_probe_result(
            ProbeResultRecord(
                fingerprint=fingerprint,
                job_id="open-job",
                case_id="case-1",
                result=ProbeResult(
                    probe_id="open-probe",
                    status=ProbeStatus.DONE,
                    processed_duration_s=duration,
                ),
                accepted_at=app.clock.now(),
            )
        )
    assert app.manager.get_view("case-1").search_progress[
        "processed_camera_seconds"
    ] == 25.0
    assert MetricsService(app.repository, app.clock).snapshot()[
        "camera_seconds_processed"
    ] == 25.0


def test_concurrent_duplicate_worker_delivery_advances_routing_once():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="concurrent-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    job = app.repository.list_jobs("case-1", states={JobState.PENDING.value})[0]
    result = app.perception_worker.process_probe(job.payload, now=app.clock.now())
    barrier = Barrier(2)

    def submit():
        barrier.wait()
        return app.manager.submit_probe_result(job.job_id, result).route_revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(lambda _: submit(), range(2)))
    assert revisions == [1, 1]
    assert len(app.repository.list_probe_results("case-1")) == 1
    app.routing_dispatcher.drain()
    assert app.manager.get_view("case-1").route_revision == 2
    runs = app.repository.list_case_data("case-1", kinds={"ROUTING_RUN"})
    assert len(runs) == 2  # initial gallery-ready tick plus one accepted result


def test_concurrent_duplicate_create_request_is_serialised_by_idempotency_key():
    app = system()
    barrier = Barrier(2)

    def create():
        barrier.wait()
        return app.manager.create_case(
            create_body(), idempotency_key="same-concurrent-create"
        ).case_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        case_ids = list(pool.map(lambda _: create(), range(2)))
    assert case_ids == ["case-1", "case-1"]
    assert len(app.repository.list_cases()) == 1
    assert len(
        [
            event
            for event in app.repository.list_events("case-1")
            if event.event_type == "CASE_CREATED"
        ]
    ) == 1


def test_sql_process_restart_preserves_case_auth_audit_and_svg(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'main.sqlite'}"
    artifact_path = tmp_path / "artifacts"
    sites = InMemorySiteConfig(RoutingParams(K=4))
    sites.add(graph())
    observations = [
        FakeObservation(
            observation_id="restart-B",
            camera_id="B",
            t_start=BASE + timedelta(seconds=20),
            t_end=BASE + timedelta(seconds=30),
            appearance_score=0.93,
            clip_ref=ArtifactRef(archive_id="archive-restart-B"),
        )
    ]

    repository = SqlAlchemyRepository.from_url(database_url)
    repository.create_schema()
    first = assemble_system(
        repository=repository,
        artifacts=FileArtifactStore(artifact_path),
        live_feed=InMemoryLiveFeedStore(),
        sites=sites,
        perception=FakePerceptionAdapter(FakePerceptionWorker(observations)),
        clip=FakeClipAdapter(FakeClipWorker()),
        retention=FakeRetentionAdapter(FakeRetentionWorker()),
        clock=FixedClock(BASE + timedelta(seconds=60)),
    )
    root = first.auth.bootstrap_root("root", "root-password-123")
    request = create_body().model_copy(update={"operator_id": root.user_id})
    first.manager.create_case(request, idempotency_key="restart-create")
    first.scheduler.run_case_once("case-1")
    before = first.manager.get_view("case-1")
    assert before.best_hypothesis is not None
    assert before.rendered_route_maps

    restarted_repository = SqlAlchemyRepository.from_url(database_url)
    restarted = assemble_system(
        repository=restarted_repository,
        artifacts=FileArtifactStore(artifact_path),
        live_feed=InMemoryLiveFeedStore(),
        sites=sites,
        perception=FakePerceptionAdapter(FakePerceptionWorker(observations)),
        clip=FakeClipAdapter(FakeClipWorker()),
        retention=FakeRetentionAdapter(FakeRetentionWorker()),
        clock=FixedClock(BASE + timedelta(seconds=61)),
    )
    after = restarted.manager.get_view("case-1")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    route = after.rendered_route_maps[0]
    assert b'data-role="best-route"' in restarted.manager.get_route_svg(
        "case-1", after.route_revision, route.floor_id
    )
    assert restarted.auth.authenticate("root", "root-password-123").actor.user_id == root.user_id
    assert any(
        event["event_type"] == "ROUTING_ADVANCED"
        for event in restarted.manager.get_events("case-1")
    )


def test_sql_close_redacts_search_payloads_but_keeps_operational_metrics(tmp_path):
    repository = SqlAlchemyRepository.from_url(
        f"sqlite:///{tmp_path / 'close-redaction.sqlite'}"
    )
    repository.create_schema()
    sites = InMemorySiteConfig(RoutingParams(K=4))
    sites.add(graph())
    app = assemble_system(
        repository=repository,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        live_feed=InMemoryLiveFeedStore(),
        sites=sites,
        perception=FakePerceptionAdapter(
            FakePerceptionWorker(
                [
                    FakeObservation(
                        observation_id="sql-close-B",
                        camera_id="B",
                        t_start=BASE + timedelta(seconds=20),
                        t_end=BASE + timedelta(seconds=30),
                        appearance_score=0.93,
                        clip_ref=ArtifactRef(archive_id="archive-sql-close-B"),
                    )
                ]
            )
        ),
        clip=FakeClipAdapter(FakeClipWorker()),
        retention=FakeRetentionAdapter(FakeRetentionWorker()),
        clock=FixedClock(BASE + timedelta(seconds=60)),
    )
    app.manager.create_case(create_body(), idempotency_key="sql-close-create")
    app.scheduler.run_case_once("case-1")
    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="sql-close",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(seconds=70),
        ),
        idempotency_key="sql-close",
    )
    app.outbox_dispatcher.drain()

    assert all(
        job.payload.target_gallery_ref == "__deleted__"
        and job.payload.probe.camera == "__deleted__"
        for job in repository.list_jobs("case-1")
    )
    assert all(
        not record.result.tracklets and not record.result.candidates
        for record in repository.list_probe_results("case-1")
    )
    metrics = MetricsService(repository, app.clock).snapshot()
    assert metrics["camera_seconds_processed"] > 0
    assert metrics["job_completion_latency"]["count"] >= 1

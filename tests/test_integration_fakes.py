from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from javes_fakes import (
    ArchiveWindow,
    FakeClipWorker,
    FakeDashboardRequests,
    FakePerceptionWorker,
    FakeRetentionWorker,
    FakeVideoArchive,
)
from javes_fakes.contracts import (
    ArtifactRef,
    CaseCommandBody,
    CaseWindow,
    ClipLifecycleState,
    ClipProcessingStatus,
    ClipStateCommand,
    CreateCaseBody,
    CutClipCommand,
    ExportClipsCommand,
    FakeHttpRequest,
    FakeObservation,
    LastKnown,
    PerceptionProbeJob,
    RetentionClass,
    RetentionCommand,
    RetentionResource,
    ReviewDecisionBody,
    TargetGalleryCommand,
    VideoAvailability,
    VideoSegmentRequest,
)
from javes_fakes.demo import build_scenario
from routing.contracts import ProbeRequest, ProbeStatus


UTC = timezone.utc
BASE = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def perception_job(*, open_probe: bool = False, camera: str = "B") -> PerceptionProbeJob:
    return PerceptionProbeJob(
        job_id="job-1",
        case_id="case-1",
        target_gallery_ref="gallery-1",
        probe=ProbeRequest(
            probe_id="probe-1",
            camera_id=camera,
            t_from=BASE,
            t_to=None if open_probe else BASE + timedelta(minutes=1),
        ),
    )


def test_fake_perception_empty_failure_and_gallery_idempotency():
    worker = FakePerceptionWorker()
    command = TargetGalleryCommand(
        command_id="gallery-command-1",
        case_id="case-1",
        source="reference",
        image_ref=ArtifactRef(object_key="cases/case-1/reference.jpg"),
    )
    first = worker.update_gallery(command)
    second = worker.update_gallery(command)
    assert first == second
    assert first.source_count == 1

    empty = worker.process_probe(perception_job(), now=BASE + timedelta(minutes=1))
    assert empty.status == ProbeStatus.DONE
    assert empty.tracklets == []
    assert empty.candidates == []
    assert empty.processed_duration_s == 60.0

    unavailable = FakePerceptionWorker(unavailable_cameras={"B"}).process_probe(
        perception_job(), now=BASE + timedelta(minutes=1)
    )
    assert unavailable.status == ProbeStatus.UNAVAILABLE
    assert unavailable.coverage_ratio == 0.0
    assert unavailable.tracklets == []


def test_fake_perception_open_upsert_and_cumulative_duration():
    worker = FakePerceptionWorker(
        [
            FakeObservation(
                observation_id="obs-1",
                camera_id="B",
                t_start=BASE + timedelta(seconds=10),
                t_end=BASE + timedelta(seconds=30),
                appearance_score=0.8,
            )
        ]
    )
    job = perception_job(open_probe=True)
    active = worker.process_probe(job, now=BASE + timedelta(seconds=20))
    closed_track = worker.process_probe(job, now=BASE + timedelta(seconds=40))
    closed_stream = worker.process_probe(
        job, now=BASE + timedelta(seconds=40), close_stream=True
    )

    assert active.tracklets[0].open is True
    assert closed_track.tracklets[0].open is False
    assert active.tracklets[0].id == closed_track.tracklets[0].id
    assert active.processed_duration_s == 20.0
    assert closed_track.processed_duration_s == 40.0
    assert closed_stream.stream_closed is True


def clip_command(request_id: str = "cut-1") -> CutClipCommand:
    return CutClipCommand(
        clip_request_id=request_id,
        case_id="case-1",
        candidate_id="candidate-1",
        tracklet_id="tracklet-1",
        camera_id="B",
        t_from=BASE,
        t_to=BASE + timedelta(seconds=20),
        source_ref=ArtifactRef(archive_id="archive-B"),
    )


def test_fake_clip_idempotent_state_and_ordered_export():
    worker = FakeClipWorker()
    first = worker.cut(clip_command())
    second = worker.cut(clip_command())
    assert first == second
    assert first.status == ClipProcessingStatus.READY
    assert first.state == ClipLifecycleState.HOLD

    confirmed = worker.change_state(
        ClipStateCommand(
            command_id="confirm-1",
            case_id="case-1",
            clip_id=first.clip_id,
            action="CONFIRM",
            operator_id="operator-1",
            occurred_at=BASE + timedelta(minutes=1),
        )
    )
    assert confirmed.state == ClipLifecycleState.CONFIRMED
    exported = worker.export(
        ExportClipsCommand(
            export_request_id="export-1",
            case_id="case-1",
            ordered_clip_ids=[first.clip_id],
        )
    )
    assert exported.status == ClipProcessingStatus.READY
    assert exported.ordered_clip_ids == [first.clip_id]
    assert exported.artifact_ref is not None

    with pytest.raises(ValueError, match="different payload"):
        worker.cut(clip_command("cut-1").model_copy(update={"camera_id": "C"}))


def test_fake_clip_unavailable_is_not_ready():
    worker = FakeClipWorker(unavailable_source_ids={"archive-B"})
    result = worker.cut(clip_command())
    assert result.status == ClipProcessingStatus.UNAVAILABLE
    assert result.state is None
    assert result.artifact_ref is None


def test_fake_retention_close_delete_and_legal_hold():
    worker = FakeRetentionWorker(
        [
            RetentionResource(
                resource_id="crop",
                case_id="case-1",
                kind="CROP",
                retention_class=RetentionClass.EPHEMERAL,
            ),
            RetentionResource(
                resource_id="route",
                case_id="case-1",
                kind="ROUTE_SVG",
                retention_class=RetentionClass.CASE_RESULT,
            ),
            RetentionResource(
                resource_id="review-hold",
                case_id="case-1",
                kind="HOLD_CLIP",
                retention_class=RetentionClass.REVIEW_HOLD,
            ),
            RetentionResource(
                resource_id="legal",
                case_id="case-1",
                kind="CONFIRMED_CLIP",
                retention_class=RetentionClass.CONFIRMED_EVIDENCE,
                legal_hold=True,
            ),
        ]
    )
    close = RetentionCommand(
        command_id="close-1",
        case_id="case-1",
        action="CLOSE_CASE",
        occurred_at=BASE,
    )
    result = worker.process(close)
    assert result.deleted_resource_ids == []
    assert result.preserved_resource_ids == ["crop", "legal", "review-hold", "route"]
    assert worker.process(close) == result

    deleted = worker.process(
        RetentionCommand(
            command_id="delete-1",
            case_id="case-1",
            action="DELETE_CASE",
            occurred_at=BASE + timedelta(minutes=1),
        )
    )
    assert deleted.deleted_resource_ids == ["crop", "review-hold", "route"]
    assert deleted.preserved_resource_ids == ["legal"]
    worker.register(
        RetentionResource(
            resource_id="late-artifact",
            case_id="case-1",
            kind="LATE_CLIP",
            retention_class=RetentionClass.EPHEMERAL,
        )
    )
    assert worker.resources["late-artifact"].deleted is True


def test_fake_retention_ttl_is_separate_and_preserves_future_or_held_data():
    worker = FakeRetentionWorker(
        [
            RetentionResource(
                resource_id="due",
                case_id="case-ttl",
                kind="CROP",
                retention_class=RetentionClass.EPHEMERAL,
                expires_at=BASE,
            ),
            RetentionResource(
                resource_id="future",
                case_id="case-ttl",
                kind="ROUTE_SVG",
                retention_class=RetentionClass.CASE_RESULT,
                expires_at=BASE + timedelta(days=1),
            ),
            RetentionResource(
                resource_id="held",
                case_id="case-ttl",
                kind="CONFIRMED_CLIP",
                retention_class=RetentionClass.CONFIRMED_EVIDENCE,
                expires_at=BASE,
                legal_hold=True,
            ),
        ]
    )
    result = worker.process(
        RetentionCommand(
            command_id="ttl-1",
            case_id="case-ttl",
            action="TTL_EXPIRED",
            occurred_at=BASE,
        )
    )
    assert result.deleted_resource_ids == ["due"]
    assert result.preserved_resource_ids == ["future", "held"]


def test_fake_dashboard_requests_and_checked_in_fixture():
    create = CreateCaseBody(
        case_id="case-1",
        operator_id="operator-1",
        target_id="target-1",
        reference_image_ref=ArtifactRef(object_key="cases/case-1/reference.jpg"),
        last_known=LastKnown(camera_id="A", time=BASE),
        window=CaseWindow(start=BASE, end=BASE + timedelta(minutes=20)),
        site_id="site-1",
        site_version="1",
    )
    requests = FakeDashboardRequests.standard_scenario(
        case=create,
        candidate_id="candidate-1",
        tracklet_id="tracklet-1",
        operator_id="operator-1",
        now=BASE,
    )
    assert [request.path for request in requests] == [
        "/v1/auth/login",
        "/v1/cases",
        "/v1/cases/case-1/view",
        "/v1/cases/case-1/decisions",
        "/v1/cases/case-1/decisions",
        "/v1/cases/case-1/commands/close",
        "/v1/cases/case-1/commands/export",
        "/v1/cases/case-1/commands/delete",
    ]
    assert all(request.headers["idempotency-key"] for request in requests[1:])

    fixture_path = Path(__file__).parents[1] / "examples" / "dashboard_requests.json"
    fixtures = [FakeHttpRequest.model_validate(item) for item in json.loads(fixture_path.read_text())]
    assert len(fixtures) == 8
    assert fixtures[0].path == "/v1/auth/login"
    CreateCaseBody.model_validate(fixtures[1].body)
    ReviewDecisionBody.model_validate(fixtures[3].body)
    ReviewDecisionBody.model_validate(fixtures[4].body)
    for fixture in fixtures[5:]:
        CaseCommandBody.model_validate(fixture.body)


def test_full_fake_contract_scenario():
    scenario = build_scenario()
    assert scenario["probe_result"]["status"] == "DONE"
    assert scenario["clip_record"]["state"] == "CONFIRMED"
    assert scenario["clip_export"]["status"] == "READY"
    assert scenario["retention_result"]["deleted_resource_ids"] == []
    decisions = [
        request["json"]["decision"]
        for request in scenario["dashboard_requests"]
        if request["path"].endswith("/decisions")
    ]
    assert decisions == ["REJECT", "CONFIRM"]


def test_fake_video_archive_reports_full_partial_and_unavailable_coverage():
    archive = FakeVideoArchive(
        [
            ArchiveWindow(
                camera_id="B",
                t_from=BASE + timedelta(seconds=10),
                t_to=BASE + timedelta(seconds=50),
                source_ref=ArtifactRef(archive_id="archive-B-1"),
            )
        ]
    )
    partial = archive.get_segment(
        VideoSegmentRequest(
            request_id="segment-1",
            camera_id="B",
            t_from=BASE,
            t_to=BASE + timedelta(seconds=60),
        ),
        now=BASE + timedelta(seconds=60),
    )
    assert partial.availability == VideoAvailability.PARTIAL
    assert partial.coverage_ratio == pytest.approx(2 / 3)
    unavailable = archive.get_segment(
        VideoSegmentRequest(
            request_id="segment-2",
            camera_id="C",
            t_from=BASE,
            t_to="OPEN",
        ),
        now=BASE + timedelta(seconds=60),
    )
    assert unavailable.availability == VideoAvailability.UNAVAILABLE

    disjoint = FakeVideoArchive(
        [
            ArchiveWindow(
                camera_id="B",
                t_from=BASE,
                t_to=BASE + timedelta(seconds=10),
                source_ref=ArtifactRef(archive_id="archive-B-early"),
            ),
            ArchiveWindow(
                camera_id="B",
                t_from=BASE + timedelta(seconds=50),
                t_to=BASE + timedelta(seconds=60),
                source_ref=ArtifactRef(archive_id="archive-B-late"),
            ),
        ]
    ).get_segment(
        VideoSegmentRequest(
            request_id="segment-disjoint",
            camera_id="B",
            t_from=BASE,
            t_to=BASE + timedelta(seconds=60),
        ),
        now=BASE + timedelta(seconds=60),
    )
    assert disjoint.availability == VideoAvailability.PARTIAL
    assert disjoint.coverage_ratio == pytest.approx(1 / 3)
    assert unavailable.coverage_ratio == 0.0

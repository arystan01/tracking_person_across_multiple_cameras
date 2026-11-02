from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from javes_contracts import (
    ArtifactRef,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    PerceptionProbeJob,
    ProbeRequest,
    ProbeResult,
    ProbeStatus,
    RetentionCommand,
    TargetGalleryCommand,
    VideoSegmentRequest,
)
from javes_contracts.acceptance import (
    ContractViolation,
    check_clip_state_result,
    check_cut_result,
    check_export_result,
    check_gallery_result,
    check_probe_result,
    check_retention_result,
    check_site_snapshot,
    check_video_segment_result,
)
from javes_fakes import (
    ArchiveWindow,
    FakeClipWorker,
    FakePerceptionWorker,
    FakeRetentionWorker,
    FakeVideoArchive,
)
from javes_fakes.contracts import FakeObservation, RetentionClass, RetentionResource
from routing.site import parse_site


UTC = timezone.utc
BASE = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def test_perception_fake_passes_reusable_acceptance_checks() -> None:
    worker = FakePerceptionWorker(
        [
            FakeObservation(
                observation_id="obs-1",
                camera_id="B",
                t_start=BASE + timedelta(seconds=10),
                t_end=BASE + timedelta(seconds=40),
                appearance_score=0.8,
            )
        ]
    )
    gallery_command = TargetGalleryCommand(
        command_id="gallery-1",
        case_id="case-1",
        source="reference",
        image_ref=ArtifactRef(object_key="cases/case-1/reference.jpg"),
    )
    check_gallery_result(gallery_command, worker.update_gallery(gallery_command))
    job = PerceptionProbeJob(
        job_id="job-1",
        case_id="case-1",
        target_gallery_ref="gallery-1",
        probe=ProbeRequest(
            probe_id="probe-1",
            camera_id="B",
            t_from=BASE,
            t_to="OPEN",
        ),
    )
    first = worker.process_probe(job, now=BASE + timedelta(seconds=20))
    second = worker.process_probe(job, now=BASE + timedelta(seconds=50))
    check_probe_result(job, first)
    check_probe_result(job, second, previous=first)


def test_probe_acceptance_rejects_cross_camera_and_error_evidence() -> None:
    job = PerceptionProbeJob(
        job_id="job-1",
        case_id="case-1",
        target_gallery_ref="gallery-1",
        probe=ProbeRequest(
            probe_id="probe-1",
            camera_id="B",
            t_from=BASE,
            t_to=BASE + timedelta(minutes=1),
        ),
    )
    bad = ProbeResult(
        probe_id="probe-1",
        status=ProbeStatus.FAILED,
        coverage_ratio=1.0,
    )
    with pytest.raises(ContractViolation, match="coverage_ratio"):
        check_probe_result(job, bad)


def test_clip_retention_and_archive_fakes_pass_acceptance_checks() -> None:
    clips = FakeClipWorker()
    cut = CutClipCommand(
        clip_request_id="cut-1",
        case_id="case-1",
        candidate_id="candidate-1",
        tracklet_id="tracklet-1",
        camera_id="B",
        t_from=BASE,
        t_to=BASE + timedelta(seconds=20),
        source_ref=ArtifactRef(archive_id="archive-B"),
    )
    record = clips.cut(cut)
    check_cut_result(cut, record)
    state_command = ClipStateCommand(
        command_id="confirm-1",
        case_id="case-1",
        clip_id=record.clip_id,
        action="CONFIRM",
        occurred_at=BASE + timedelta(minutes=1),
    )
    confirmed = clips.change_state(state_command)
    check_clip_state_result(state_command, confirmed, previous=record)
    export_command = ExportClipsCommand(
        export_request_id="export-1",
        case_id="case-1",
        ordered_clip_ids=[record.clip_id],
    )
    check_export_result(export_command, clips.export(export_command))

    retention = FakeRetentionWorker(
        [
            RetentionResource(
                resource_id="temporary-1",
                case_id="case-1",
                kind="CROP",
                retention_class=RetentionClass.EPHEMERAL,
            )
        ]
    )
    retention_command = RetentionCommand(
        command_id="close-1",
        case_id="case-1",
        action="CLOSE_CASE",
        occurred_at=BASE,
    )
    check_retention_result(
        retention_command, retention.process(retention_command)
    )

    source = ArtifactRef(archive_id="archive-B")
    archive = FakeVideoArchive(
        [ArchiveWindow("B", BASE, BASE + timedelta(minutes=2), source)]
    )
    segment_request = VideoSegmentRequest(
        request_id="segment-1",
        camera_id="B",
        t_from=BASE,
        t_to=BASE + timedelta(minutes=1),
    )
    check_video_segment_result(
        segment_request,
        archive.get_segment(segment_request, now=BASE + timedelta(minutes=2)),
    )


def test_site_snapshot_requires_declared_floor_asset() -> None:
    graph = parse_site(
        {
            "site": "site-1",
            "version": "1",
            "map": {
                "floors": [
                    {"id": "1F", "image": "maps/1F.png", "size_px": [100, 100]}
                ]
            },
            "cameras": {"A": {"floor": "1F", "map_xy": [20, 20]}},
            "edges": [],
        }
    )
    with pytest.raises(ContractViolation, match="missing floor asset"):
        check_site_snapshot(graph, {})
    check_site_snapshot(graph, {"1F": b"image-bytes"})


"""Print a deterministic boundary scenario for Main Infra development."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from routing.contracts import ProbeRequest

from .clip import FakeClipWorker
from .contracts import (
    ArtifactRef,
    CaseCommandBody,
    CaseWindow,
    ClipStateCommand,
    CreateCaseBody,
    CutClipCommand,
    ExportClipsCommand,
    FakeObservation,
    LastKnown,
    PerceptionProbeJob,
    RetentionClass,
    RetentionCommand,
    RetentionResource,
    ReviewDecisionBody,
    TargetGalleryCommand,
)
from .dashboard import FakeDashboardRequests
from .perception import FakePerceptionWorker
from .retention import FakeRetentionWorker


def build_scenario() -> dict:
    base = datetime(2026, 10, 16, 5, 16, tzinfo=timezone.utc)
    case_id = "case-demo-001"
    operator_id = "operator-demo"
    perception = FakePerceptionWorker(
        [
            FakeObservation(
                observation_id="observation-B-1",
                camera_id="B",
                t_start=base + timedelta(seconds=52),
                t_end=base + timedelta(seconds=71),
                appearance_score=0.68,
                cosine=0.57,
                bbox_first=(38, 210, 64, 150),
                bbox_last=(598, 190, 70, 160),
                clip_ref=ArtifactRef(archive_id="archive-B-20261016"),
            )
        ]
    )
    gallery = perception.update_gallery(
        TargetGalleryCommand(
            command_id="gallery-command-001",
            case_id=case_id,
            source="reference",
            image_ref=ArtifactRef(object_key="cases/case-demo-001/reference.jpg"),
        )
    )
    probe = ProbeRequest(
        probe_id="probe-B-001",
        camera_id="B",
        t_from=base,
        t_to=base + timedelta(minutes=2),
        priority=0.72,
        reason="ROUTE_EXPANSION",
        hypothesis_ids=["hypothesis-001"],
    )
    job = PerceptionProbeJob(
        job_id="job-B-001",
        case_id=case_id,
        target_gallery_ref=gallery.gallery_ref,
        probe=probe,
    )
    probe_result = perception.process_probe(job, now=base + timedelta(minutes=2))
    tracklet = probe_result.tracklets[0]
    candidate = probe_result.candidates[0]

    clip_worker = FakeClipWorker()
    clip = clip_worker.cut(
        CutClipCommand(
            clip_request_id="clip-request-001",
            case_id=case_id,
            candidate_id=candidate.id or "candidate",
            tracklet_id=tracklet.id,
            camera_id=tracklet.camera,
            t_from=tracklet.t_in - timedelta(seconds=5),
            t_to=tracklet.t_out + timedelta(seconds=5),
            padding_before_s=5,
            padding_after_s=5,
            source_ref=ArtifactRef(archive_id="archive-B-20261016"),
        )
    )
    clip = clip_worker.change_state(
        ClipStateCommand(
            command_id="clip-confirm-001",
            case_id=case_id,
            clip_id=clip.clip_id,
            action="CONFIRM",
            operator_id=operator_id,
            occurred_at=base + timedelta(minutes=5),
        )
    )
    export = clip_worker.export(
        ExportClipsCommand(
            export_request_id="export-001",
            case_id=case_id,
            ordered_clip_ids=[clip.clip_id],
        )
    )

    retention = FakeRetentionWorker(
        [
            RetentionResource(
                resource_id="crop-001",
                case_id=case_id,
                kind="CROP",
                retention_class=RetentionClass.EPHEMERAL,
            ),
            RetentionResource(
                resource_id=clip.clip_id,
                case_id=case_id,
                kind="CONFIRMED_CLIP",
                retention_class=RetentionClass.CONFIRMED_EVIDENCE,
                artifact_ref=clip.artifact_ref,
            ),
            RetentionResource(
                resource_id="route-001",
                case_id=case_id,
                kind="ROUTE_SVG",
                retention_class=RetentionClass.CASE_RESULT,
            ),
        ]
    )
    retention_result = retention.process(
        RetentionCommand(
            command_id="retention-close-001",
            case_id=case_id,
            action="CLOSE_CASE",
            actor_id=operator_id,
            occurred_at=base + timedelta(minutes=6),
        )
    )

    create_case = CreateCaseBody(
        case_id=case_id,
        operator_id=operator_id,
        target_id="target-demo",
        reference_image_ref=ArtifactRef(
            object_key="cases/case-demo-001/reference.jpg"
        ),
        last_known=LastKnown(camera_id="A", time=base),
        window=CaseWindow(start=base, end=base + timedelta(minutes=20)),
        site_id="demo-site",
        site_version="1",
    )
    dashboard_requests = FakeDashboardRequests.standard_scenario(
        case=create_case,
        candidate_id=candidate.id or "candidate",
        tracklet_id=tracklet.id,
        operator_id=operator_id,
        now=base + timedelta(minutes=5),
    )
    return {
        "target_gallery": gallery.model_dump(mode="json"),
        "perception_job": job.model_dump(mode="json"),
        "probe_result": probe_result.model_dump(mode="json"),
        "clip_record": clip.model_dump(mode="json"),
        "clip_export": export.model_dump(mode="json"),
        "retention_result": retention_result.model_dump(mode="json"),
        "dashboard_requests": [item.model_dump(mode="json") for item in dashboard_requests],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build_scenario(),
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

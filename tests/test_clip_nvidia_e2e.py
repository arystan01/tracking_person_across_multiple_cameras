from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import shutil

import pytest

from javes_archive import NvidiaArchiveRegistry, create_nvidia_archive_app
from javes_clip.archive import HttpClipArchive
from javes_clip.storage import ClipStore
from javes_clip.worker import ClipWorker
from javes_contracts import (
    ArtifactRef,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    RetentionCommand,
    RetentionResource,
)
from javes_main.adapters.artifacts import FileArtifactStore
from javes_retention import build_local_retention_worker
from tests.support.real_cv_e2e import SyncASGIClient


ROOT = Path(__file__).resolve().parents[1]
NVIDIA = ROOT / "data/nvidia_mtmc/mtmc_12cam"
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.real_cv
def test_real_nvidia_clip_cut_export_and_retention(tmp_path: Path) -> None:
    if os.environ.get("JAVES_RUN_REAL_CV") != "1":
        pytest.skip("set JAVES_RUN_REAL_CV=1 for the NVIDIA Clip E2E")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required")

    archive_http = SyncASGIClient(
        create_nvidia_archive_app(
            NvidiaArchiveRegistry(NVIDIA, verify=True),
            service_token="archive-secret",
        ),
        base_url="http://archive",
    )
    archive = HttpClipArchive(
        "http://archive",
        "archive-secret",
        archive_root=NVIDIA,
        client=archive_http,
    )
    artifact_root = tmp_path / "artifacts"
    store = ClipStore(tmp_path / "state" / "clip.sqlite", artifact_root)
    worker = ClipWorker(store, archive)
    try:
        clips = []
        for index, camera in enumerate(("Cam001", "Cam003"), start=1):
            command = CutClipCommand(
                clip_request_id=f"nvidia-cut-{index}",
                case_id="nvidia-clip-case",
                candidate_id=f"candidate-{index}",
                tracklet_id=f"tracklet-{index}",
                camera_id=camera,
                # A 5–7s sighting with the default two seconds of context.
                # Main has already expanded the window; Clip must not pad twice.
                t_from=ORIGIN + timedelta(seconds=3),
                t_to=ORIGIN + timedelta(seconds=9),
                padding_before_s=2,
                padding_after_s=2,
                source_ref=ArtifactRef(
                    archive_id=f"nvidia-mtmc-12cam:mtmc-12cam-v1:{camera}"
                ),
            )
            clip = worker.cut(command)
            assert clip.status == "READY"
            assert clip.duration_s == pytest.approx(6.0, abs=0.05)
            clips.append(
                worker.change_state(
                    ClipStateCommand(
                        command_id=f"confirm-{index}",
                        case_id=command.case_id,
                        clip_id=clip.clip_id,
                        action="CONFIRM",
                        occurred_at=ORIGIN + timedelta(seconds=8),
                    )
                )
            )

        export = worker.export(
            ExportClipsCommand(
                export_request_id="nvidia-export",
                case_id="nvidia-clip-case",
                ordered_clip_ids=[item.clip_id for item in clips],
            )
        )
        assert export.status == "READY"
        assert export.duration_s == pytest.approx(12.0, abs=0.1)
        assert export.artifact_ref is not None

        artifacts = FileArtifactStore(artifact_root)
        retention = build_local_retention_worker(
            database_path=tmp_path / "state" / "retention.sqlite",
            artifact_store=artifacts,
            clock=lambda: ORIGIN + timedelta(days=15),
        )
        try:
            for result in [*clips, export]:
                manifest = worker.manifest(result)[0]
                retention.register(
                    RetentionResource(
                        resource_id="resource-" + manifest.sha256,
                        case_id="nvidia-clip-case",
                        kind=manifest.kind.upper(),
                        retention_class=manifest.policy_class(),
                        artifact_ref=ArtifactRef(
                            object_key=manifest.object_key,
                            sha256=manifest.sha256,
                        ),
                        revision=manifest.policy_revision,
                    )
                )
            deleted = retention.process(
                RetentionCommand(
                    command_id="delete-nvidia-clip-case",
                    case_id="nvidia-clip-case",
                    action="TTL_EXPIRED",
                    occurred_at=ORIGIN + timedelta(days=15),
                )
            )
            assert len(deleted.deleted_resource_ids) == 3
            assert not [path for path in artifact_root.rglob("*.mp4")]
        finally:
            retention.close()
    finally:
        store.close()

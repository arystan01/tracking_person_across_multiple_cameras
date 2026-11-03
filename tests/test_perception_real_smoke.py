"""Opt-in smoke test using the tracked videos and actual CV models.

Run in the dedicated real-CV CI job with ``JAVES_RUN_REAL_CV=1``.  There are
deliberately no detector/embedder/archive test doubles in this module.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.real_cv


def test_tracked_models_produce_real_probe_result(tmp_path):
    if os.environ.get("JAVES_RUN_REAL_CV") != "1":
        pytest.skip("set JAVES_RUN_REAL_CV=1 in the real-CV environment")
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("ultralytics")
    pytest.importorskip("torchreid")

    from javes_contracts import ArtifactRef, PerceptionProbeJob, TargetGalleryCommand
    from javes_main.adapters.artifacts import FileArtifactStore
    from javes_perception import PerceptionWorker
    from javes_perception.config import load_settings
    from routing.contracts import ProbeRequest, ProbeStatus

    root = Path(__file__).resolve().parents[1]
    assert (root / "models/yolo26n.pt").is_file()
    assert (root / "models/osnet_ain_x1_0_msmt17.pt").is_file()
    video = root / "data/videos/people-detection.mp4"
    capture = cv2.VideoCapture(str(video))
    ok, frame = capture.read()
    capture.release()
    assert ok
    encoded_ok, encoded = cv2.imencode(".jpg", frame)
    assert encoded_ok

    artifacts = FileArtifactStore(tmp_path / "artifacts")
    reference_key = "cases/real-cv/reference.jpg"
    artifacts.put(reference_key, encoded.tobytes(), "image/jpeg")
    worker = PerceptionWorker(load_settings(root / "perception.yaml"), artifacts)
    gallery = worker.update_gallery(
        TargetGalleryCommand(
            command_id="real-cv-gallery",
            case_id="real-cv",
            source="reference",
            image_ref=ArtifactRef(object_key=reference_key),
            model_version=worker.settings.model_version,
        )
    )
    start = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
    result = worker.execute(
        PerceptionProbeJob(
            job_id="real-cv-job",
            case_id="real-cv",
            target_gallery_ref=gallery.gallery_ref,
            probe=ProbeRequest(
                probe_id="real-cv-probe",
                camera="cam_1",
                t_from=start,
                t_to=start + timedelta(seconds=10),
            ),
        ),
        start + timedelta(seconds=10),
    )
    assert result.status == ProbeStatus.DONE
    assert result.processed_duration_s > 0
    assert result.n_tracklets_total > 0
    assert result.tracklets
    assert all(item.embedding_path for item in result.tracklets)

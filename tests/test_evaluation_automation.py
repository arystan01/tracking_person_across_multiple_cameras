from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_has_fast_cached_and_scheduled_cold_jobs() -> None:
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "matrix:" in workflow
    assert 'python-version: ["3.11", "3.13"]' in workflow
    assert "cached-real-cv:" in workflow
    assert "cold-real-cv:" in workflow
    assert "schedule:" in workflow
    assert "JAVES_REAL_CV_REFRESH: \"1\"" in workflow
    assert "actions/cache@v4" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "--junitxml=" in workflow
    assert "continue-on-error" not in workflow


def test_extraction_fingerprint_covers_every_invalidation_input() -> None:
    pytest.importorskip("numpy")
    from javes_perception.core.extraction import extraction_fingerprint

    base = {
        "video_sha256": "video",
        "detector_sha256": "detector",
        "reid_sha256": "reid",
        "tracker_sha256": "tracker",
        "camera_id": "Cam001",
        "t_from": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "t_to": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        "sample_fps": 5.0,
        "image_size": 1280,
        "tracker_config": {"track_buffer": 40},
        "calibration_checksum": "calibration",
        "perception_version": "extractor-v1",
    }
    original = extraction_fingerprint(**base)
    changes = {
        "video_sha256": "video-2",
        "detector_sha256": "detector-2",
        "reid_sha256": "reid-2",
        "tracker_sha256": "tracker-2",
        "camera_id": "Cam002",
        "t_from": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        "t_to": datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
        "sample_fps": 4.0,
        "image_size": 960,
        "tracker_config": {"track_buffer": 41},
        "calibration_checksum": "calibration-2",
        "perception_version": "extractor-v2",
    }
    for field, value in changes.items():
        changed = dict(base)
        changed[field] = value
        assert extraction_fingerprint(**changed) != original, field


def test_extraction_cache_rejects_bundle_stored_under_wrong_fingerprint(
    tmp_path: Path,
) -> None:
    pytest.importorskip("numpy")
    from javes_perception.core.extraction import (
        ExtractionBundle,
        FilesystemExtractionCache,
    )

    expected = "a" * 64
    actual = "b" * 64
    bundle = ExtractionBundle(
        fingerprint=actual,
        camera_id="Cam001",
        t_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        t_to=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        requested_s=60.0,
        archive_id="nvidia:Cam001",
        timestamps=(),
        tracks=(),
    )
    cache = FilesystemExtractionCache(tmp_path)
    wrong_path = tmp_path / expected[:2] / f"{expected}.zip"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(bundle.to_bytes())

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        cache.get(expected)


def test_refresh_cache_ignores_previous_run_but_reuses_current_run(
    tmp_path: Path,
) -> None:
    pytest.importorskip("numpy")
    from javes_perception.core.extraction import (
        ExtractionBundle,
        FilesystemExtractionCache,
    )

    fingerprint = "c" * 64
    bundle = ExtractionBundle(
        fingerprint=fingerprint,
        camera_id="Cam001",
        t_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        t_to=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        requested_s=60.0,
        archive_id="nvidia:Cam001",
        timestamps=(),
        tracks=(),
    )
    warm = FilesystemExtractionCache(tmp_path)
    warm.put(bundle)
    cold = FilesystemExtractionCache(tmp_path, refresh=True)
    assert cold.get(fingerprint) is None
    cold.put(bundle)
    assert cold.get(fingerprint) == bundle

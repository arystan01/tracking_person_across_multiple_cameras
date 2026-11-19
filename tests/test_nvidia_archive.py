from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from javes_archive import NvidiaArchiveManifest, NvidiaArchiveRegistry
from javes_archive.api import create_nvidia_archive_app
from javes_contracts import VideoAvailability, VideoSegmentRequest
from javes_contracts.acceptance import check_video_segment_result
from tests.support.real_cv_e2e import SyncASGIClient


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/nvidia_mtmc/mtmc_12cam"
MANIFEST = FIXTURE / "archive-manifest.v1.json"
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request(
    camera_id: str,
    start_s: float,
    end_s: float | str,
    *,
    request_id: str = "archive-test",
) -> VideoSegmentRequest:
    return VideoSegmentRequest(
        request_id=request_id,
        camera_id=camera_id,
        t_from=ORIGIN + timedelta(seconds=start_s),
        t_to=(
            end_s
            if end_s == "OPEN"
            else ORIGIN + timedelta(seconds=float(end_s))
        ),
    )


def test_manifest_pins_all_sources_and_verifies_real_checksums() -> None:
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted((FIXTURE / "videos").glob("*.mp4"))
    }
    registry = NvidiaArchiveRegistry(FIXTURE, verify=True)
    assert set(registry.manifest.cameras) == {
        f"Cam{index:03d}" for index in range(1, 13)
    }
    assert registry.verified
    assert len(registry.verify_sources()) == 12
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted((FIXTURE / "videos").glob("*.mp4"))
    }
    assert after == before


def test_exact_partial_open_unavailable_and_stable_source_reference() -> None:
    registry = NvidiaArchiveRegistry(FIXTURE)

    exact_request = _request("Cam001", 5.0, 10.0)
    exact = registry.segment(exact_request)
    check_video_segment_result(exact_request, exact)
    assert exact.availability == VideoAvailability.AVAILABLE
    assert exact.coverage_ratio == 1.0
    assert exact.covered_from == ORIGIN + timedelta(seconds=5)
    assert exact.covered_to == ORIGIN + timedelta(seconds=10)
    assert exact.source_offset_s == 5.0
    assert exact.source_ref is not None
    assert exact.source_ref.archive_id == "nvidia-mtmc-12cam:mtmc-12cam-v1:Cam001"
    assert exact.source_ref.object_key == "videos/Warehouse_Synthetic_Cam001.mp4"

    repeated = registry.segment(
        exact_request.model_copy(update={"request_id": "archive-repeat"})
    )
    assert repeated.source_ref == exact.source_ref
    assert repeated.source_offset_s == exact.source_offset_s

    partial_request = _request("Cam001", -5.0, 5.0)
    partial = registry.segment(partial_request)
    check_video_segment_result(partial_request, partial)
    assert partial.availability == VideoAvailability.PARTIAL
    assert partial.coverage_ratio == 0.5
    assert partial.covered_from == ORIGIN
    assert partial.covered_to == ORIGIN + timedelta(seconds=5)
    assert partial.source_offset_s == 0.0

    open_request = _request("Cam001", 18.0, "OPEN")
    opened = registry.segment(open_request)
    check_video_segment_result(open_request, opened)
    assert opened.availability == VideoAvailability.AVAILABLE
    assert opened.covered_to == ORIGIN + timedelta(seconds=20.066667)
    assert opened.source_offset_s == 18.0

    for request, code in (
        (_request("Cam001", 21.0, 22.0), "VIDEO_UNAVAILABLE"),
        (_request("Cam999", 0.0, 1.0), "CAMERA_UNKNOWN"),
    ):
        result = registry.segment(request)
        check_video_segment_result(request, result)
        assert result.availability == VideoAvailability.UNAVAILABLE
        assert result.error_code == code


def test_api_requires_auth_and_inspect_never_discloses_paths() -> None:
    registry = NvidiaArchiveRegistry(FIXTURE)
    client = SyncASGIClient(
        create_nvidia_archive_app(registry, service_token="archive-secret"),
        base_url="http://archive",
    )
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").json()["cameras"] == 12
    assert client.get("/internal/v1/archive/inspect").status_code == 401
    response = client.get(
        "/internal/v1/archive/inspect",
        headers={"authorization": "Bearer archive-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["manifest_checksum"] == registry.manifest_checksum
    assert len(body["cameras"]) == 12
    encoded = json.dumps(body)
    assert str(FIXTURE) not in encoded
    assert "relative_path" not in encoded
    assert "Warehouse_Synthetic" not in encoded

    request = _request("Cam012", 0.0, 1.0)
    segment = client.post(
        "/v1/video-segments",
        headers={"authorization": "Bearer archive-secret"},
        json=request.model_dump(mode="json"),
    )
    assert segment.status_code == 200
    assert segment.json()["source_clock"] == "UTC_CORRECTED"


def test_manifest_rejects_absolute_parent_and_latest_values() -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["cameras"]["Cam001"]["relative_path"] = "../outside.mp4"
    with pytest.raises(ValidationError, match="safe POSIX path"):
        NvidiaArchiveManifest.model_validate(raw)

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["cameras"]["Cam001"]["relative_path"] = "/tmp/outside.mp4"
    with pytest.raises(ValidationError, match="safe POSIX path"):
        NvidiaArchiveManifest.model_validate(raw)

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["version"] = "latest"
    with pytest.raises(ValidationError, match="cannot be latest"):
        NvidiaArchiveManifest.model_validate(raw)


def test_registry_rejects_source_symlink_even_when_it_resolves_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "archive"
    videos = root / "videos"
    videos.mkdir(parents=True)
    target = videos / "target.mp4"
    target.write_bytes(b"immutable-video")
    link = videos / "linked.mp4"
    link.symlink_to(target.name)
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for item in raw["cameras"].values():
        item["relative_path"] = "videos/target.mp4"
        item["size_bytes"] = len(b"immutable-video")
    raw["cameras"]["Cam001"]["relative_path"] = "videos/linked.mp4"
    manifest = root / "archive-manifest.v1.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    registry = NvidiaArchiveRegistry(root)
    with pytest.raises(ValueError, match="uses a symlink"):
        registry.segment(_request("Cam001", 0.0, 1.0))


def test_timeline_offset_is_applied_once_by_archive_registry(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    videos = root / "videos"
    videos.mkdir(parents=True)
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for camera_id, item in raw["cameras"].items():
        destination = videos / Path(item["relative_path"]).name
        destination.write_bytes(b"fixture")
        if camera_id == "Cam001":
            item["timeline_offset_s"] = 10.0
    manifest = root / "archive-manifest.v1.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    registry = NvidiaArchiveRegistry(root)
    result = registry.segment(_request("Cam001", 10.0, 12.0))
    assert result.availability == VideoAvailability.AVAILABLE
    assert result.covered_from == ORIGIN + timedelta(seconds=10)
    assert result.source_offset_s == 0.0


def test_http_archive_preserves_manifest_source_offset() -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from javes_perception.runtime.archive_api import HttpVideoArchive

    registry = NvidiaArchiveRegistry(FIXTURE)
    client = SyncASGIClient(
        create_nvidia_archive_app(registry, service_token="archive-secret"),
        base_url="http://testserver",
    )
    archive = HttpVideoArchive(
        "http://testserver",
        "archive-secret",
        camera_ids={"Cam001"},
        artifact_root=FIXTURE,
        client=client,
    )
    window = archive.window(
        "Cam001",
        ORIGIN + timedelta(seconds=5),
        ORIGIN + timedelta(seconds=6),
    )
    assert window is not None and window.source_offset_s == 5.0
    observed_at, actual = next(archive.frames(window, sample_fps=1.0))
    assert observed_at == ORIGIN + timedelta(seconds=5)

    source = FIXTURE / "videos/Warehouse_Synthetic_Cam001.mp4"
    capture = cv2.VideoCapture(str(source))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, 150)
        ok, expected = capture.read()
    finally:
        capture.release()
    assert ok
    assert np.mean(np.abs(actual.astype("int16") - expected.astype("int16"))) < 1.0

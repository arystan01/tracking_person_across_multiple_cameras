from datetime import datetime, timedelta, timezone

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from javes_contracts import ArtifactRef, PerceptionProbeJob, TargetGalleryCommand
from javes_contracts.acceptance import check_gallery_result, check_probe_result
from javes_main.adapters.artifacts import InMemoryArtifactStore
from javes_perception import CameraSource, PerceptionSettings, PerceptionWorker
from javes_perception.core.models import _byte_tracker_args
from javes_perception.core.extraction import (
    FilesystemExtractionCache,
    extraction_fingerprint,
)
from javes_perception.core.worker import trajectory_from_path
from javes_perception.runtime.state import PerceptionStateStore
from javes_perception.runtime.video import DecodeError, VideoWindow
from javes_contracts import ProbeRequest, ProbeStatus

T0 = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)
RED = (0, 0, 255)
BLUE = (255, 0, 0)


class FakeArchive:
    def __init__(self, seconds=20, fps=5, broken=False):
        self.seconds = seconds
        self.fps = fps
        self.broken = broken

    def has(self, camera_id):
        return camera_id == "cam_1"

    def window(self, camera_id, t_from, t_to):
        end = min(t_to, T0 + timedelta(seconds=self.seconds))
        start = max(t_from, T0)
        if end <= start:
            return None
        return VideoWindow(
            camera_id,
            start,
            end,
            (t_to - t_from).total_seconds(),
            "archive_cam_1",
            content_sha256="0" * 64,
        )

    def frames(self, window, sample_fps):
        if self.broken:
            raise DecodeError("broken")
        t = window.t_from
        while t <= window.t_to:
            sec = (t - T0).total_seconds()
            frame = np.zeros((200, 300, 3), np.uint8)
            if 2 <= sec <= 8:
                frame[20:180, 20:80] = RED
            if 5 <= sec <= 20:
                frame[20:180, 200:260] = BLUE
            yield t, frame
            t += timedelta(seconds=1 / self.fps)


class FakeDetector:
    def reset(self):
        pass

    def detect(self, frame):
        return [((0.0, 0.0, float(frame.shape[1]), float(frame.shape[0])), 0.9)]

    def track(self, frame):
        found = []
        if frame[100, 50].any():
            found.append((1, (20.0, 20.0, 80.0, 180.0), 0.9))
        if frame[100, 230].any():
            found.append((2, (200.0, 20.0, 260.0, 180.0), 0.9))
        return found


class EdgeClippedArchive:
    def has(self, camera_id):
        return camera_id == "cam_1"

    def window(self, camera_id, t_from, t_to):
        return VideoWindow(
            camera_id,
            t_from,
            min(t_to, t_from + timedelta(seconds=1)),
            1.0,
            "edge-clipped",
        )

    def frames(self, window, sample_fps):
        for index in range(6):
            frame = np.zeros((200, 300, 3), np.uint8)
            frame[:] = RED
            yield window.t_from + timedelta(seconds=index / 5), frame


class EdgeClippedDetector:
    def __init__(self):
        self.index = 0

    def reset(self):
        self.index = 0

    def detect(self, frame):
        return [((0.0, 0.0, float(frame.shape[1]), float(frame.shape[0])), 0.9)]

    def track(self, frame):
        x1 = 0.0 if self.index in {2, 3} else 20.0
        self.index += 1
        return [(1, (x1, 20.0, x1 + 60.0, 180.0), 0.9)]


def fake_embedder(crops):
    out = []
    for crop in crops:
        b, g, r = crop.reshape(-1, 3).mean(axis=0)
        v = np.array([r, b, 10.0], np.float32)
        out.append(v / np.linalg.norm(v))
    return np.stack(out)


def make_worker(archive=None):
    settings = PerceptionSettings(
        cameras={"cam_1": CameraSource(path="unused.mp4", start=T0)},
        min_crop_height=10,
        score_floor=0.1,
    )
    artifacts = InMemoryArtifactStore()
    worker = PerceptionWorker(
        settings, artifacts, detector=FakeDetector(), embedder=fake_embedder, archive=archive or FakeArchive()
    )
    return worker, artifacts


def add_reference(worker, artifacts, color=RED):
    import cv2

    image = np.zeros((100, 40, 3), np.uint8)
    image[:] = color
    ok, jpeg = cv2.imencode(".png", image)
    artifacts.put("cases/c1/reference.png", jpeg.tobytes(), "image/png")
    command = TargetGalleryCommand(
        command_id="g1", case_id="c1", source="reference", image_ref=ArtifactRef(object_key="cases/c1/reference.png")
    )
    result = worker.update_gallery(command)
    check_gallery_result(command, result)
    return result.gallery_ref


def job(gallery_ref, t_from=0, t_to=20, deadline=None, probe_id="p1", camera="cam_1"):
    return PerceptionProbeJob(
        job_id="j1",
        case_id="c1",
        target_gallery_ref=gallery_ref,
        probe=ProbeRequest(
            probe_id=probe_id,
            camera=camera,
            t_from=T0 + timedelta(seconds=t_from),
            t_to=None if t_to is None else T0 + timedelta(seconds=t_to),
        ),
        deadline=deadline,
    )


def test_target_ranks_above_other_person():
    worker, artifacts = make_worker()
    probe = job(add_reference(worker, artifacts))
    result = worker.execute(probe, T0 + timedelta(minutes=1))
    check_probe_result(probe, result)
    assert result.status == ProbeStatus.DONE
    assert result.n_tracklets_total == 2
    assert len(result.candidates) == 1 and result.candidates[0].score > 0.9
    target = result.tracklets[0]
    assert target.t_in == T0 + timedelta(seconds=2)
    assert target.bbox_first == (20.0, 20.0, 60.0, 160.0)
    assert target.trajectory
    assert target.trajectory[0].x_px == 50.0
    assert target.trajectory[0].y_px == 180.0
    assert target.trajectory[0].confidence == 0.9
    assert target.crop_paths and artifacts.get(target.crop_paths[0])
    assert result.coverage_ratio == pytest.approx(1.0, abs=0.02)


def test_trajectory_downsampling_is_deterministic_and_keeps_endpoints():
    path = [
        (
            T0 + timedelta(milliseconds=index * 40),
            (float(index), 10.0, float(index + 20), 50.0),
        )
        for index in range(1000)
    ]
    first = trajectory_from_path(path, [0.8] * len(path))
    second = trajectory_from_path(path, [0.8] * len(path))
    assert first == second
    assert len(first) == 256
    assert first[0].timestamp == path[0][0]
    assert first[-1].timestamp == path[-1][0]
    assert [point.timestamp for point in first] == sorted(
        point.timestamp for point in first
    )


def test_trajectory_clamps_tracked_foot_point_to_visible_frame_boundary():
    path = [
        (T0, (-4.0, 900.0, 36.0, 1081.8)),
        (T0 + timedelta(milliseconds=200), (1900.0, 910.0, 1932.0, 1083.0)),
    ]

    trajectory = trajectory_from_path(
        path,
        [0.8, 0.7],
        frame_size_px=(1920, 1080),
    )

    assert [(point.x_px, point.y_px) for point in trajectory] == [
        (18.0, 1080.0),
        (1910.0, 1080.0),
    ]


@pytest.mark.parametrize("sample_fps", [1.5, 5.0])
def test_bytetrack_lost_buffer_uses_wall_clock_time(sample_fps):
    pytest.importorskip("ultralytics")
    from ultralytics.engine.results import Boxes
    from ultralytics.trackers.byte_tracker import BYTETracker

    def detections(rows):
        values = np.asarray(rows, dtype=np.float32).reshape(-1, 6)
        return Boxes(values, (1080, 1920)).cpu().numpy()

    def ids(output):
        values = np.asarray(output)
        if values.size == 0:
            return []
        return [int(row[4]) for row in np.atleast_2d(values)]

    tracker = BYTETracker(
        _byte_tracker_args("bytetrack.yaml", sample_fps, 8.0)
    )
    first_id = ids(
        tracker.update(detections([[100, 100, 150, 250, 0.9, 0]]))
    )[0]
    for _ in range(int(sample_fps * 7.0)):
        tracker.update(detections([]))
    assert ids(
        tracker.update(detections([[100, 100, 150, 250, 0.9, 0]]))
    ) == [first_id]


@pytest.mark.parametrize("sample_fps", [1.5, 5.0])
def test_bytetrack_does_not_reuse_id_after_wall_clock_buffer(sample_fps):
    pytest.importorskip("ultralytics")
    from ultralytics.engine.results import Boxes
    from ultralytics.trackers.byte_tracker import BYTETracker

    def detections(rows):
        values = np.asarray(rows, dtype=np.float32).reshape(-1, 6)
        return Boxes(values, (1080, 1920)).cpu().numpy()

    def ids(output):
        values = np.asarray(output)
        if values.size == 0:
            return []
        return [int(row[4]) for row in np.atleast_2d(values)]

    tracker = BYTETracker(
        _byte_tracker_args("bytetrack.yaml", sample_fps, 8.0)
    )
    first_id = ids(
        tracker.update(detections([[100, 100, 150, 250, 0.9, 0]]))
    )[0]
    for _ in range(int(sample_fps * 9.0) + 1):
        tracker.update(detections([]))
    tracker.update(detections([[100, 100, 150, 250, 0.9, 0]]))
    replacement = ids(
        tracker.update(detections([[100, 100, 150, 250, 0.9, 0]]))
    )
    assert replacement and replacement[0] != first_id


def test_edge_clipped_detections_stay_in_confirmed_trajectory_not_embedding():
    settings = PerceptionSettings(
        cameras={"cam_1": CameraSource(path="unused.mp4", start=T0)},
        sample_fps=5.0,
        min_crop_height=10,
        embed_every=1,
        score_floor=0.1,
    )
    artifacts = InMemoryArtifactStore()
    embedded_crops = []

    def recording_embedder(crops):
        embedded_crops.extend(crops)
        return fake_embedder(crops)

    worker = PerceptionWorker(
        settings,
        artifacts,
        detector=EdgeClippedDetector(),
        embedder=recording_embedder,
        archive=EdgeClippedArchive(),
    )
    result = worker.execute(
        job(add_reference(worker, artifacts), t_from=0, t_to=1),
        T0 + timedelta(seconds=2),
    )
    assert result.status == ProbeStatus.DONE
    assert len(result.tracklets) == 1
    assert len(result.tracklets[0].trajectory or []) == 6
    # One reference crop plus four non-edge track crops were embedded.  The
    # two clipped detections remain geometric evidence but are not Re-ID input.
    assert len(embedded_crops) == 5


def test_retry_gives_same_ids():
    worker, artifacts = make_worker()
    probe = job(add_reference(worker, artifacts))
    first = worker.execute(probe, T0 + timedelta(minutes=1))
    second = worker.execute(probe, T0 + timedelta(minutes=1))
    assert [t.id for t in first.tracklets] == [t.id for t in second.tracklets]
    assert [c.id for c in first.candidates] == [c.id for c in second.candidates]


def test_extraction_cache_is_target_independent_and_survives_restart(tmp_path):
    cache = FilesystemExtractionCache(tmp_path / "extraction-cache")
    worker, artifacts = make_worker()
    worker.extraction_cache = cache
    reference = add_reference(worker, artifacts)
    probe = job(reference)
    first = worker.execute(probe, T0 + timedelta(minutes=1))
    assert worker.extraction_cache_misses == 1
    assert worker.extraction_cache_hits == 0

    class CacheOnlyDetector(FakeDetector):
        def track(self, frame):
            raise AssertionError("cached extraction must not rerun detector")

    restarted = PerceptionWorker(
        worker.settings,
        artifacts,
        detector=CacheOnlyDetector(),
        embedder=fake_embedder,
        archive=FakeArchive(),
        extraction_cache=cache,
    )
    second = restarted.execute(probe, T0 + timedelta(minutes=1))
    assert restarted.extraction_cache_hits == 1
    assert restarted.extraction_cache_misses == 0
    assert second == first


def test_extraction_fingerprint_excludes_target_but_pins_calibration():
    common = {
        "video_sha256": "video",
        "detector_sha256": "detector",
        "reid_sha256": "reid",
        "tracker_sha256": "tracker",
        "camera_id": "cam_1",
        "t_from": T0,
        "t_to": T0 + timedelta(seconds=20),
        "sample_fps": 5.0,
        "image_size": 1280,
        "tracker_config": {"buffer_s": 8.0},
    }
    first = extraction_fingerprint(
        **common, calibration_checksum="calibration-a"
    )
    same = extraction_fingerprint(
        **common, calibration_checksum="calibration-a"
    )
    changed = extraction_fingerprint(
        **common, calibration_checksum="calibration-b"
    )
    assert first == same
    assert changed != first


def test_empty_window_is_done_empty():
    worker, artifacts = make_worker(FakeArchive(seconds=40))
    probe = job(add_reference(worker, artifacts), t_from=25, t_to=40)
    result = worker.execute(probe, T0 + timedelta(minutes=1))
    check_probe_result(probe, result)
    assert result.status == ProbeStatus.DONE
    assert result.tracklets == [] and result.n_tracklets_total == 0
    assert result.processed_duration_s > 0


def test_partial_coverage():
    worker, artifacts = make_worker()
    probe = job(add_reference(worker, artifacts), t_from=10, t_to=30)
    result = worker.execute(probe, T0 + timedelta(minutes=1))
    check_probe_result(probe, result)
    assert 0.4 < result.coverage_ratio < 0.6


def test_error_statuses():
    worker, artifacts = make_worker()
    ref = add_reference(worker, artifacts)
    now = T0 + timedelta(minutes=1)
    cases = [
        (job(ref, t_from=100, t_to=200), ProbeStatus.UNAVAILABLE, "VIDEO_UNAVAILABLE"),
        (job(ref, camera="cam_9"), ProbeStatus.UNAVAILABLE, "CAMERA_UNKNOWN"),
        (job("gallery_missing"), ProbeStatus.FAILED, "GALLERY_UNAVAILABLE"),
        (job(ref, deadline=T0), ProbeStatus.TIMEOUT, "DEADLINE_EXPIRED"),
    ]
    for probe, status, code in cases:
        result = worker.execute(probe, now)
        check_probe_result(probe, result)
        assert (result.status, result.error_code) == (status, code)

    broken, artifacts = make_worker(FakeArchive(broken=True))
    probe = job(add_reference(broken, artifacts))
    result = broken.execute(probe, now)
    assert (result.status, result.error_code) == (ProbeStatus.FAILED, "DECODE_FAILED")


def test_open_probe_updates_and_close():
    worker, artifacts = make_worker()
    probe = job(add_reference(worker, artifacts), t_to=None)
    first = worker.execute(probe, T0 + timedelta(seconds=7))
    check_probe_result(probe, first)
    assert any(t.open for t in first.tracklets)
    second = worker.execute(probe, T0 + timedelta(seconds=15))
    check_probe_result(probe, second, previous=first)
    closed = worker.cancel(probe, T0 + timedelta(seconds=16))
    check_probe_result(probe, closed, previous=second)
    assert closed.stream_closed and not any(t.open for t in closed.tracklets)
    assert worker.execute(probe, T0 + timedelta(seconds=18)) == closed


def test_gallery_is_idempotent_and_survives_restart():
    worker, artifacts = make_worker()
    ref = add_reference(worker, artifacts)
    assert add_reference(worker, artifacts) == ref
    probe = job(ref)
    result = worker.execute(probe, T0 + timedelta(minutes=1))
    best = max(result.candidates, key=lambda c: c.score)

    restarted = PerceptionWorker(
        worker.settings, artifacts, detector=FakeDetector(), embedder=fake_embedder, archive=FakeArchive()
    )
    again = restarted.execute(probe, T0 + timedelta(minutes=1))
    assert again.status == ProbeStatus.DONE
    confirmed = restarted.update_gallery(
        TargetGalleryCommand(command_id="g2", case_id="c1", source="confirmed_tracklet", tracklet_id=best.tracklet_id)
    )
    assert confirmed.source_count == 2 and confirmed.gallery_ref != ref
    with pytest.raises(ValueError):
        restarted.update_gallery(
            TargetGalleryCommand(command_id="g2", case_id="c1", source="confirmed_tracklet", tracklet_id="other")
        )


def test_open_probe_before_its_start_is_done_empty():
    worker, artifacts = make_worker()
    probe = job(add_reference(worker, artifacts), t_from=10, t_to=None)
    result = worker.execute(probe, T0 + timedelta(seconds=5))
    check_probe_result(probe, result)
    assert result.status == ProbeStatus.DONE and result.processed_duration_s == 0.0


def test_open_checkpoint_and_gallery_command_survive_process_restart(tmp_path):
    state = PerceptionStateStore(tmp_path / "perception-state.sqlite")
    settings = PerceptionSettings(
        cameras={"cam_1": CameraSource(path="unused.mp4", start=T0)},
        min_crop_height=10,
        score_floor=0.1,
    )
    artifacts = InMemoryArtifactStore()
    first_worker = PerceptionWorker(
        settings,
        artifacts,
        detector=FakeDetector(),
        embedder=fake_embedder,
        archive=FakeArchive(),
        state_store=state,
    )
    ref = add_reference(first_worker, artifacts)
    open_job = job(ref, t_to=None)
    first = first_worker.execute(open_job, T0 + timedelta(seconds=7))
    assert first.status == ProbeStatus.DONE
    assert state.checkpoint(open_job.job_id) == T0 + timedelta(seconds=7)
    state.close()

    restarted_state = PerceptionStateStore(tmp_path / "perception-state.sqlite")
    restarted = PerceptionWorker(
        settings,
        artifacts,
        detector=FakeDetector(),
        embedder=fake_embedder,
        archive=FakeArchive(),
        state_store=restarted_state,
    )
    second = restarted.execute(open_job, T0 + timedelta(seconds=15))
    assert second.status == ProbeStatus.DONE
    assert second.processed_duration_s >= first.processed_duration_s
    check_probe_result(open_job, second, previous=first)
    first_by_id = {tracklet.id: tracklet for tracklet in first.tracklets}
    extended = [
        tracklet
        for tracklet in second.tracklets
        if tracklet.id in first_by_id
        and tracklet.trajectory
        and first_by_id[tracklet.id].trajectory
        and tracklet.trajectory[-1].timestamp
        > first_by_id[tracklet.id].trajectory[-1].timestamp
    ]
    assert extended
    for tracklet in extended:
        timestamps = [point.timestamp for point in tracklet.trajectory or []]
        assert timestamps == sorted(set(timestamps))
        previous_points = first_by_id[tracklet.id].trajectory or []
        assert tracklet.trajectory[: len(previous_points)] == previous_points
    assert restarted.update_gallery(
        TargetGalleryCommand(
            command_id="g1",
            case_id="c1",
            source="reference",
            image_ref=ArtifactRef(object_key="cases/c1/reference.png"),
        )
    ).gallery_ref == ref
    with pytest.raises(ValueError):
        restarted.update_gallery(
            TargetGalleryCommand(
                command_id="g1",
                case_id="c1",
                source="confirmed_tracklet",
                tracklet_id="different",
            )
        )
    restarted_state.close()

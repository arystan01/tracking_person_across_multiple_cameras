from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np

from javes_contracts import PerceptionProbeJob, TargetGalleryCommand, TargetGalleryResult
from javes_contracts.boundary import aware_utc
from routing.contracts import Candidate, ProbeResult, ProbeStatus, Tracklet

from .config import PerceptionSettings
from .models import normalize
from .video import DecodeError, LocalVideoArchive


def _stable_id(prefix: str, *parts: object) -> str:
    source = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


def _to_npy(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array.astype(np.float32))
    return buffer.getvalue()


def _from_npy(data: bytes) -> np.ndarray:
    return np.load(io.BytesIO(data))


def _xywh(xyxy: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = xyxy
    return (round(x1, 1), round(y1, 1), round(x2 - x1, 1), round(y2 - y1, 1))


def _crop(frame: np.ndarray, xyxy: tuple[float, float, float, float]) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = xyxy
    x1, y1 = max(int(x1), 0), max(int(y1), 0)
    x2, y2 = min(int(x2), w), min(int(y2), h)
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


@dataclass
class _Track:
    first_ts: datetime
    last_ts: datetime
    bbox_first: tuple[float, float, float, float]
    bbox_last: tuple[float, float, float, float]
    frames: int = 0
    good_frames: int = 0
    best_crop: np.ndarray | None = None
    best_quality: float = 0.0
    features: list[np.ndarray] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    path: list[tuple[datetime, tuple[float, float, float, float]]] = field(default_factory=list)


class PerceptionWorker:
    def __init__(
        self,
        settings: PerceptionSettings,
        artifacts,
        *,
        detector=None,
        embedder=None,
        archive=None,
    ) -> None:
        self.settings = settings
        self.artifacts = artifacts
        self._detector = detector
        self._embedder = embedder
        self.archive = archive or LocalVideoArchive(settings.cameras)
        self._galleries: dict[str, np.ndarray] = {}
        self._gallery_sources: dict[str, dict[str, np.ndarray]] = {}
        self._gallery_commands: dict[str, tuple[str, TargetGalleryResult]] = {}
        self._closed: dict[str, ProbeResult] = {}

    @classmethod
    def from_config(cls, path, artifacts) -> PerceptionWorker:
        from .config import load_settings

        return cls(load_settings(path), artifacts)

    @property
    def detector(self):
        if self._detector is None:
            from .models import YoloByteTrack, pick_device

            s = self.settings
            self._detector = YoloByteTrack(
                s.detector_weights, s.tracker, pick_device(s.device), s.det_conf, s.imgsz
            )
        return self._detector

    @property
    def embedder(self):
        if self._embedder is None:
            from .models import OsnetEmbedder, pick_device

            s = self.settings
            self._embedder = OsnetEmbedder(s.reid_model, s.reid_weights, pick_device(s.device))
        return self._embedder

    def _put(self, key: str, data: bytes, mime_type: str) -> str:
        try:
            self.artifacts.put(key, data, mime_type)
        except ValueError:
            pass
        return key

    def _gallery_key(self, case_id: str, gallery_ref: str) -> str:
        return f"cases/{case_id}/perception/gallery/{gallery_ref}.npy"

    def _embedding_key(self, case_id: str, tracklet_id: str) -> str:
        return f"cases/{case_id}/perception/embeddings/{tracklet_id}.npy"

    def update_gallery(self, command: TargetGalleryCommand) -> TargetGalleryResult:
        fingerprint = command.model_dump_json()
        previous = self._gallery_commands.get(command.command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same gallery command_id used with different payload")
            return previous[1].model_copy(deep=True)

        if command.source == "reference":
            source_id = command.image_ref.identity()
            vector = self._embed_reference(command)
        else:
            source_id = f"tracklet:{command.tracklet_id}"
            data, _ = self.artifacts.get(self._embedding_key(command.case_id, command.tracklet_id))
            vector = _from_npy(data)

        sources = self._load_sources(command.case_id)
        if source_id not in sources:
            sources[source_id] = vector
            buffer = io.BytesIO()
            np.savez(buffer, name=np.array(source_id), vector=vector.astype(np.float32))
            key = f"cases/{command.case_id}/perception/gallery/sources/{len(sources) - 1:04d}.npz"
            self.artifacts.put(key, buffer.getvalue(), "application/x-npz")
        names = sorted(sources)
        gallery_ref = _stable_id("gallery", command.case_id, command.model_version, *names)
        matrix = np.stack([sources[name] for name in names])
        self._galleries[gallery_ref] = matrix
        self._put(self._gallery_key(command.case_id, gallery_ref), _to_npy(matrix), "application/x-npy")
        result = TargetGalleryResult(
            command_id=command.command_id,
            case_id=command.case_id,
            gallery_ref=gallery_ref,
            source_count=len(names),
            model_version=command.model_version,
        )
        self._gallery_commands[command.command_id] = (fingerprint, result)
        return result.model_copy(deep=True)

    def _load_sources(self, case_id: str) -> dict[str, np.ndarray]:
        if case_id not in self._gallery_sources:
            sources: dict[str, np.ndarray] = {}
            while True:
                key = f"cases/{case_id}/perception/gallery/sources/{len(sources):04d}.npz"
                try:
                    data, _ = self.artifacts.get(key)
                except KeyError:
                    break
                saved = np.load(io.BytesIO(data))
                sources[str(saved["name"])] = saved["vector"]
            self._gallery_sources[case_id] = sources
        return self._gallery_sources[case_id]

    def _embed_reference(self, command: TargetGalleryCommand) -> np.ndarray:
        if not command.image_ref.object_key:
            raise ValueError("reference image needs object_key in ArtifactStore")
        data, _ = self.artifacts.get(command.image_ref.object_key)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("reference image cannot be decoded")
        people = self.detector.detect(image)
        crop = image
        if people:
            xyxy, _ = max(people, key=lambda p: (p[0][2] - p[0][0]) * (p[0][3] - p[0][1]))
            crop = _crop(image, xyxy)
            if crop is None:
                crop = image
        return self.embedder([crop])[0]

    def _gallery(self, job: PerceptionProbeJob) -> np.ndarray | None:
        ref = job.target_gallery_ref
        if ref not in self._galleries:
            try:
                data, _ = self.artifacts.get(self._gallery_key(job.case_id, ref))
            except KeyError:
                return None
            self._galleries[ref] = _from_npy(data)
        return self._galleries[ref]

    def execute(self, job: PerceptionProbeJob, now: datetime) -> ProbeResult:
        return self._process(job, aware_utc(now), close=False)

    def cancel(self, job: PerceptionProbeJob, now: datetime) -> ProbeResult:
        return self._process(job, aware_utc(now), close=job.probe.t_to is None)

    def _empty(self, probe_id: str, status: ProbeStatus, code: str, close: bool) -> ProbeResult:
        return ProbeResult(
            probe_id=probe_id,
            status=status,
            coverage_ratio=0.0,
            processed_duration_s=0.0,
            n_tracklets_total=0,
            stream_closed=close,
            error_code=code,
        )

    def _process(self, job: PerceptionProbeJob, now: datetime, close: bool) -> ProbeResult:
        probe = job.probe
        if probe.probe_id in self._closed:
            return self._closed[probe.probe_id].model_copy(deep=True)
        if job.deadline is not None and now > job.deadline:
            return self._empty(probe.probe_id, ProbeStatus.TIMEOUT, "DEADLINE_EXPIRED", close)
        gallery = self._gallery(job)
        if gallery is None:
            return self._empty(probe.probe_id, ProbeStatus.FAILED, "GALLERY_UNAVAILABLE", close)
        if not self.archive.has(probe.camera):
            return self._empty(probe.probe_id, ProbeStatus.UNAVAILABLE, "CAMERA_UNKNOWN", close)

        end = probe.t_to or now
        if probe.t_to is None and end <= probe.t_from:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=ProbeStatus.DONE,
                coverage_ratio=0.0,
                processed_duration_s=0.0,
                n_tracklets_total=0,
                stream_closed=close,
            )
        try:
            window = self.archive.window(probe.camera, probe.t_from, end)
        except DecodeError:
            window = None
        if window is None:
            return self._empty(probe.probe_id, ProbeStatus.UNAVAILABLE, "VIDEO_UNAVAILABLE", close)

        try:
            tracks, timestamps = self._track(window)
        except DecodeError:
            return self._empty(probe.probe_id, ProbeStatus.FAILED, "DECODE_FAILED", close)
        except Exception:
            return self._empty(probe.probe_id, ProbeStatus.FAILED, "MODEL_FAILED", close)
        if not timestamps:
            return self._empty(probe.probe_id, ProbeStatus.FAILED, "DECODE_FAILED", close)

        result = self._build_result(job, window, tracks, timestamps, gallery, close)
        if close:
            self._closed[probe.probe_id] = result
        return result

    def _track(self, window) -> tuple[dict[int, _Track], list[datetime]]:
        s = self.settings
        detector = self.detector
        detector.reset()
        tracks: dict[int, _Track] = {}
        timestamps: list[datetime] = []
        for ts, frame in self.archive.frames(window, s.sample_fps):
            ts = min(max(ts, window.t_from), window.t_to)
            timestamps.append(ts)
            batch: list[tuple[_Track, np.ndarray, float]] = []
            people = detector.track(frame)
            boxes = [xyxy for _, xyxy, _ in people]
            for index, (track_id, xyxy, conf) in enumerate(people):
                box = _xywh(xyxy)
                track = tracks.get(track_id)
                if track is None:
                    track = _Track(first_ts=ts, last_ts=ts, bbox_first=box, bbox_last=box)
                    tracks[track_id] = track
                track.last_ts = ts
                track.bbox_last = box
                track.frames += 1
                track.path.append((ts, xyxy))
                quality = self._crop_quality(frame, xyxy, conf, boxes[:index] + boxes[index + 1 :])
                if quality <= 0:
                    continue
                crop = _crop(frame, xyxy)
                if crop is None:
                    continue
                if quality > track.best_quality:
                    track.best_quality = quality
                    track.best_crop = crop.copy()
                track.good_frames += 1
                if (track.good_frames - 1) % s.embed_every == 0:
                    batch.append((track, crop, quality))
            if batch:
                features = self.embedder([crop for _, crop, _ in batch])
                for (track, _, quality), feature in zip(batch, features):
                    track.features.append(feature)
                    track.weights.append(quality)
        return tracks, timestamps

    def _crop_quality(self, frame, xyxy, conf, others) -> float:
        s = self.settings
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = xyxy
        bw, bh = x2 - x1, y2 - y1
        if bh < s.min_crop_height or bw <= 0:
            return 0.0
        if not s.crop_filter:
            return 1.0
        if conf < s.min_embed_conf:
            return 0.0
        if not 1.2 <= bh / bw <= 4.5:
            return 0.0
        margin = s.edge_margin_px
        if x1 < margin or y1 < margin or x2 > w - margin or y2 > h - margin:
            return 0.0
        for ox1, oy1, ox2, oy2 in others:
            iw = max(0.0, min(x2, ox2) - max(x1, ox1))
            ih = max(0.0, min(y2, oy2) - max(y1, oy1))
            if iw * ih > s.max_overlap * bw * bh:
                return 0.0
        return conf * min(bh / 256.0, 1.0)

    def _track_embedding(self, track: _Track) -> np.ndarray:
        features = np.stack(track.features)
        weights = np.array(track.weights)
        mean = normalize((features * weights[:, None]).sum(axis=0))
        if len(features) >= 4:
            closeness = features @ mean
            keep = closeness >= np.quantile(closeness, 0.2)
            mean = normalize((features[keep] * weights[keep, None]).sum(axis=0))
        return mean

    def _score(self, cosine: float) -> float:
        s = self.settings
        return float(1.0 / (1.0 + np.exp(-(s.calib_a * cosine + s.calib_b))))

    def _build_result(self, job, window, tracks, timestamps, gallery, close) -> ProbeResult:
        s = self.settings
        probe = job.probe
        if len(timestamps) > 1:
            step = (timestamps[-1] - timestamps[0]).total_seconds() / (len(timestamps) - 1)
        else:
            step = 1.0 / s.sample_fps
        window_s = (window.t_to - window.t_from).total_seconds()
        processed = min((timestamps[-1] - timestamps[0]).total_seconds() + step, window_s)
        coverage = min(processed / window.requested_s, 1.0) if window.requested_s > 0 else 0.0

        people = [t for t in tracks.values() if t.frames >= s.min_track_frames]
        people.sort(key=lambda t: (t.first_ts, t.bbox_first))
        tracklets: list[Tracklet] = []
        candidates: list[Candidate] = []
        for track in people:
            if not track.features:
                continue
            embedding = self._track_embedding(track)
            cosine = float(np.max(gallery @ embedding))
            score = self._score(cosine)
            if score < s.score_floor:
                continue
            tracklet_id = _stable_id(
                "trk", job.case_id, probe.camera, track.first_ts.isoformat(), track.bbox_first
            )
            embedding_key = self._put(
                self._embedding_key(job.case_id, tracklet_id), _to_npy(embedding), "application/x-npy"
            )
            crop_paths = []
            if track.best_crop is not None:
                ok, jpeg = cv2.imencode(".jpg", track.best_crop)
                if ok:
                    crop_paths.append(
                        self._put(
                            f"cases/{job.case_id}/perception/crops/{tracklet_id}.jpg",
                            jpeg.tobytes(),
                            "image/jpeg",
                        )
                    )
            at_start = (track.first_ts - window.t_from).total_seconds() <= 1.5 * step
            at_end = (window.t_to - track.last_ts).total_seconds() <= 1.5 * step
            tracklets.append(
                Tracklet(
                    id=tracklet_id,
                    case_id=job.case_id,
                    probe_id=probe.probe_id,
                    camera=probe.camera,
                    t_in=track.first_ts,
                    t_out=track.last_ts,
                    bbox_first=track.bbox_first,
                    bbox_last=track.bbox_last,
                    open=probe.t_to is None and not close and at_end,
                    clipped_in=at_start,
                    clipped_out=at_end,
                    crop_paths=crop_paths,
                    embedding_path=embedding_key,
                    clip_ref={"archive_id": window.archive_id},
                )
            )
            candidates.append(
                Candidate(
                    id=_stable_id("cand", job.case_id, tracklet_id),
                    tracklet_id=tracklet_id,
                    score=round(score, 4),
                    cosine=round(cosine, 4),
                    calibration_version=s.calibration_version,
                )
            )
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.DONE,
            tracklets=tracklets,
            candidates=candidates,
            coverage_ratio=round(coverage, 4),
            processed_duration_s=round(processed, 3),
            n_tracklets_total=len(people),
            stream_closed=close,
        )

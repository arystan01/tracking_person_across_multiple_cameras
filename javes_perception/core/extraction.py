"""Target-independent Perception extraction bundles and durable cache."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from javes_contracts.boundary import aware_utc


EXTRACTION_FORMAT_VERSION = "javes-perception-extraction-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_identity(value: str | Path) -> str:
    path = Path(value)
    return sha256_file(path) if path.is_file() else hashlib.sha256(str(value).encode()).hexdigest()


def extraction_fingerprint(
    *,
    video_sha256: str,
    detector_sha256: str,
    reid_sha256: str,
    tracker_sha256: str,
    camera_id: str,
    t_from: datetime,
    t_to: datetime,
    sample_fps: float,
    image_size: int,
    tracker_config: dict[str, object],
    calibration_checksum: str,
    perception_version: str = EXTRACTION_FORMAT_VERSION,
) -> str:
    payload = {
        "calibration_checksum": calibration_checksum,
        "camera_id": camera_id,
        "detector_sha256": detector_sha256,
        "image_size": image_size,
        "perception_version": perception_version,
        "reid_sha256": reid_sha256,
        "sample_fps": sample_fps,
        "t_from": aware_utc(t_from).isoformat(),
        "t_to": aware_utc(t_to).isoformat(),
        "tracker_config": tracker_config,
        "tracker_sha256": tracker_sha256,
        "video_sha256": video_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExtractedTrack:
    source_track_id: int
    first_ts: datetime
    last_ts: datetime
    bbox_first: tuple[float, float, float, float]
    bbox_last: tuple[float, float, float, float]
    frame_size_px: tuple[int, int]
    frames: int
    good_frames: int
    embedding: np.ndarray | None
    best_crop_jpeg: bytes | None
    path: tuple[tuple[datetime, tuple[float, float, float, float]], ...]
    path_confidences: tuple[float | None, ...]


@dataclass(frozen=True)
class ExtractionBundle:
    fingerprint: str
    camera_id: str
    t_from: datetime
    t_to: datetime
    requested_s: float
    archive_id: str
    timestamps: tuple[datetime, ...]
    tracks: tuple[ExtractedTrack, ...]

    def to_bytes(self) -> bytes:
        manifest = {
            "format_version": EXTRACTION_FORMAT_VERSION,
            "fingerprint": self.fingerprint,
            "camera_id": self.camera_id,
            "t_from": self.t_from.isoformat(),
            "t_to": self.t_to.isoformat(),
            "requested_s": self.requested_s,
            "archive_id": self.archive_id,
            "timestamps": [item.isoformat() for item in self.timestamps],
            "tracks": [],
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, track in enumerate(self.tracks):
                embedding_name = None
                crop_name = None
                if track.embedding is not None:
                    embedding_name = f"embeddings/{index:05d}.npy"
                    data = io.BytesIO()
                    np.save(data, track.embedding.astype(np.float32))
                    archive.writestr(embedding_name, data.getvalue())
                if track.best_crop_jpeg is not None:
                    crop_name = f"crops/{index:05d}.jpg"
                    archive.writestr(crop_name, track.best_crop_jpeg)
                manifest["tracks"].append(
                    {
                        "source_track_id": track.source_track_id,
                        "first_ts": track.first_ts.isoformat(),
                        "last_ts": track.last_ts.isoformat(),
                        "bbox_first": track.bbox_first,
                        "bbox_last": track.bbox_last,
                        "frame_size_px": track.frame_size_px,
                        "frames": track.frames,
                        "good_frames": track.good_frames,
                        "embedding": embedding_name,
                        "best_crop": crop_name,
                        "path": [
                            [timestamp.isoformat(), bbox]
                            for timestamp, bbox in track.path
                        ],
                        "path_confidences": track.path_confidences,
                    }
                )
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            )
        return output.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> ExtractionBundle:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format_version") != EXTRACTION_FORMAT_VERSION:
                raise ValueError("unsupported Perception extraction bundle version")
            tracks = []
            for item in manifest["tracks"]:
                embedding = None
                if item["embedding"] is not None:
                    embedding = np.load(io.BytesIO(archive.read(item["embedding"])))
                crop = (
                    archive.read(item["best_crop"])
                    if item["best_crop"] is not None
                    else None
                )
                tracks.append(
                    ExtractedTrack(
                        source_track_id=int(item["source_track_id"]),
                        first_ts=aware_utc(datetime.fromisoformat(item["first_ts"])),
                        last_ts=aware_utc(datetime.fromisoformat(item["last_ts"])),
                        bbox_first=tuple(item["bbox_first"]),
                        bbox_last=tuple(item["bbox_last"]),
                        frame_size_px=tuple(item["frame_size_px"]),
                        frames=int(item["frames"]),
                        good_frames=int(item["good_frames"]),
                        embedding=embedding,
                        best_crop_jpeg=crop,
                        path=tuple(
                            (
                                aware_utc(datetime.fromisoformat(timestamp)),
                                tuple(bbox),
                            )
                            for timestamp, bbox in item["path"]
                        ),
                        path_confidences=tuple(item["path_confidences"]),
                    )
                )
        return cls(
            fingerprint=manifest["fingerprint"],
            camera_id=manifest["camera_id"],
            t_from=aware_utc(datetime.fromisoformat(manifest["t_from"])),
            t_to=aware_utc(datetime.fromisoformat(manifest["t_to"])),
            requested_s=float(manifest["requested_s"]),
            archive_id=manifest["archive_id"],
            timestamps=tuple(
                aware_utc(datetime.fromisoformat(item))
                for item in manifest["timestamps"]
            ),
            tracks=tuple(tracks),
        )


class ExtractionCache(Protocol):
    def get(self, fingerprint: str) -> ExtractionBundle | None: ...

    def put(self, bundle: ExtractionBundle) -> None: ...


class FilesystemExtractionCache:
    """Content-addressed cache; safe to share between target evaluations."""

    def __init__(self, root: Path, *, refresh: bool = False) -> None:
        self.root = root
        self.refresh = refresh
        # A cold evaluation must ignore bundles left by earlier runs, while still
        # sharing each freshly extracted bundle between targets in this process.
        self._written_this_run: set[str] = set()

    def _path(self, fingerprint: str) -> Path:
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("invalid extraction fingerprint")
        return self.root / fingerprint[:2] / f"{fingerprint}.zip"

    def get(self, fingerprint: str) -> ExtractionBundle | None:
        if self.refresh and fingerprint not in self._written_this_run:
            return None
        path = self._path(fingerprint)
        if not path.is_file():
            return None
        bundle = ExtractionBundle.from_bytes(path.read_bytes())
        if bundle.fingerprint != fingerprint:
            raise ValueError("extraction cache fingerprint mismatch")
        return bundle

    def put(self, bundle: ExtractionBundle) -> None:
        path = self._path(bundle.fingerprint)
        if path.is_file() and (
            not self.refresh or bundle.fingerprint in self._written_this_run
        ):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(bundle.to_bytes())
        temporary.replace(path)
        self._written_this_run.add(bundle.fingerprint)

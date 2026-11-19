"""Filesystem-backed, read-only registry for the NVIDIA archive."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from javes_contracts import (
    ArtifactRef,
    VideoAvailability,
    VideoSegmentRequest,
    VideoSegmentResult,
)

from .manifest import ArchiveCamera, CoverageWindow, NvidiaArchiveManifest


class NvidiaArchiveRegistry:
    """Resolve immutable source objects without mutating or copying videos."""

    def __init__(
        self,
        root: str | Path,
        manifest_path: str | Path | None = None,
        *,
        verify: bool = False,
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        candidate = (
            Path(manifest_path)
            if manifest_path is not None
            else self.root / "archive-manifest.v1.json"
        ).resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("archive manifest escapes dataset root") from exc
        self.manifest_path = candidate
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        self.manifest = NvidiaArchiveManifest.model_validate(raw)
        self.manifest_checksum = hashlib.sha256(
            self.manifest.model_dump_json(by_alias=True).encode("utf-8")
        ).hexdigest()
        self.verified = False
        if verify:
            self.verify_sources()

    def _source_path(self, camera_id: str) -> Path:
        camera = self.manifest.cameras[camera_id]
        unresolved = self.root / camera.relative_path
        cursor = self.root
        for part in Path(camera.relative_path).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"archive source for {camera_id} uses a symlink")
        resolved = unresolved.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"archive source for {camera_id} escapes dataset root") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"archive source for {camera_id} is not a file")
        return resolved

    def verify_sources(self) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for camera_id, metadata in sorted(self.manifest.cameras.items()):
            path = self._source_path(camera_id)
            size = path.stat().st_size
            if size != metadata.size_bytes:
                raise ValueError(
                    f"archive size mismatch for {camera_id}: "
                    f"expected {metadata.size_bytes}, got {size}"
                )
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            if actual != metadata.sha256:
                raise ValueError(f"archive checksum mismatch for {camera_id}")
            checksums[camera_id] = actual
        self.verified = True
        return checksums

    def _coverage_bounds(
        self,
        camera: ArchiveCamera,
        window: CoverageWindow,
    ) -> tuple[datetime, datetime]:
        start = self.manifest.timeline_origin + timedelta(
            seconds=camera.timeline_offset_s + window.start_offset_s
        )
        end = self.manifest.timeline_origin + timedelta(
            seconds=camera.timeline_offset_s + window.end_offset_s
        )
        return start, end

    def segment(self, request: VideoSegmentRequest) -> VideoSegmentResult:
        camera = self.manifest.cameras.get(request.camera_id)
        if camera is None:
            return self._unavailable(request, "CAMERA_UNKNOWN")
        self._source_path(request.camera_id)
        requested_end = (
            max(self._coverage_bounds(camera, item)[1] for item in camera.coverage)
            if request.t_to == "OPEN"
            else request.t_to
        )
        requested_seconds = max(
            (requested_end - request.t_from).total_seconds(), 0.0
        )
        if requested_seconds <= 0.0:
            return self._unavailable(request, "EMPTY_WINDOW")
        overlaps: list[tuple[float, datetime, datetime, CoverageWindow]] = []
        for window in camera.coverage:
            available_from, available_to = self._coverage_bounds(camera, window)
            covered_from = max(request.t_from, available_from)
            covered_to = min(requested_end, available_to)
            duration = (covered_to - covered_from).total_seconds()
            if duration > 0.0:
                overlaps.append((duration, covered_from, covered_to, window))
        if not overlaps:
            return self._unavailable(request, "VIDEO_UNAVAILABLE")
        duration, covered_from, covered_to, source_window = max(
            overlaps,
            key=lambda item: (item[0], -item[1].timestamp()),
        )
        ratio = min(max(duration / requested_seconds, 0.0), 1.0)
        availability = (
            VideoAvailability.AVAILABLE
            if ratio >= 1.0 - 1e-9
            else VideoAvailability.PARTIAL
        )
        source_window_from, _ = self._coverage_bounds(camera, source_window)
        source_offset_s = source_window.start_offset_s + (
            covered_from - source_window_from
        ).total_seconds()
        archive_id = (
            f"nvidia-mtmc-12cam:{self.manifest.version}:{request.camera_id}"
        )
        return VideoSegmentResult(
            request_id=request.request_id,
            camera_id=request.camera_id,
            availability=availability,
            requested_from=request.t_from,
            requested_to=request.t_to,
            covered_from=covered_from,
            covered_to=covered_to,
            coverage_ratio=round(ratio, 12),
            source_ref=ArtifactRef(
                archive_id=archive_id,
                object_key=camera.relative_path,
                sha256=camera.sha256,
            ),
            source_offset_s=round(source_offset_s, 9),
        )

    @staticmethod
    def _unavailable(
        request: VideoSegmentRequest,
        error_code: str,
    ) -> VideoSegmentResult:
        return VideoSegmentResult(
            request_id=request.request_id,
            camera_id=request.camera_id,
            availability=VideoAvailability.UNAVAILABLE,
            requested_from=request.t_from,
            requested_to=request.t_to,
            coverage_ratio=0.0,
            error_code=error_code,
        )

    def inspect(self) -> dict:
        """Return safe metadata only; source filesystem paths stay private."""
        cameras = []
        for camera_id, item in sorted(self.manifest.cameras.items()):
            coverage = [
                {
                    "from": self._coverage_bounds(item, window)[0].isoformat(),
                    "to": self._coverage_bounds(item, window)[1].isoformat(),
                }
                for window in item.coverage
            ]
            cameras.append(
                {
                    "camera_id": camera_id,
                    "archive_id": (
                        f"nvidia-mtmc-12cam:{self.manifest.version}:{camera_id}"
                    ),
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "container": item.container,
                    "codec": item.codec,
                    "resolution_px": list(item.resolution_px),
                    "fps": item.fps,
                    "duration_s": item.duration_s,
                    "coverage": coverage,
                }
            )
        return {
            "manifest_version": self.manifest.manifest_version,
            "manifest_checksum": self.manifest_checksum,
            "dataset": self.manifest.dataset,
            "version": self.manifest.version,
            "source_clock": self.manifest.source_clock,
            "verified": self.verified,
            "cameras": cameras,
        }

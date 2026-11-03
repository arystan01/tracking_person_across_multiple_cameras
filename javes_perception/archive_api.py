"""Read-only HTTP adapter for the external CCTV Video Archive."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import cv2
import httpx

from javes_contracts import (
    VideoAvailability,
    VideoSegmentRequest,
    VideoSegmentResult,
)

from .video import DecodeError, VideoWindow


class HttpVideoArchive:
    """Resolve exact camera windows and decode the returned read-only source.

    The configured endpoint accepts ``VideoSegmentRequest`` and returns
    ``VideoSegmentResult``. The result must include either an HTTP/file URI or
    a shared-filesystem object key. JAVES-AI never mutates the archive source.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        camera_ids: set[str],
        artifact_root: str | Path | None = None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.camera_ids = set(camera_ids)
        self.artifact_root = (
            Path(artifact_root).resolve() if artifact_root is not None else None
        )
        self._headers = {"authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=timeout_s)

    def has(self, camera_id: str) -> bool:
        return camera_id in self.camera_ids

    def _source_uri(self, result: VideoSegmentResult) -> str:
        if result.source_ref is None:
            raise DecodeError("CCTV Archive result has no source_ref")
        if result.source_ref.uri:
            parsed = urlparse(result.source_ref.uri)
            if parsed.scheme == "file":
                return unquote(parsed.path)
            if parsed.scheme in {"http", "https", "rtsp"}:
                return result.source_ref.uri
            raise DecodeError("CCTV Archive returned an unsupported source URI")
        if result.source_ref.object_key and self.artifact_root is not None:
            path = (self.artifact_root / result.source_ref.object_key).resolve()
            try:
                path.relative_to(self.artifact_root)
            except ValueError as exc:
                raise DecodeError("CCTV Archive object_key escapes artifact root") from exc
            return str(path)
        raise DecodeError("CCTV Archive source_ref is not decodable")

    def window(
        self, camera_id: str, t_from: datetime, t_to: datetime
    ) -> VideoWindow | None:
        request_id = "archive_" + hashlib.sha256(
            f"{camera_id}\x1f{t_from.isoformat()}\x1f{t_to.isoformat()}".encode()
        ).hexdigest()[:24]
        request = VideoSegmentRequest(
            request_id=request_id,
            camera_id=camera_id,
            t_from=t_from,
            t_to=t_to,
        )
        response = self._client.post(
            f"{self.base_url}/v1/video-segments",
            headers=self._headers,
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        result = VideoSegmentResult.model_validate(response.json())
        if result.request_id != request_id or result.camera_id != camera_id:
            raise DecodeError("CCTV Archive response correlation mismatch")
        if result.availability == VideoAvailability.UNAVAILABLE:
            return None
        if result.covered_from is None or result.covered_to is None:
            raise DecodeError("available CCTV Archive result has no covered window")
        if result.covered_to <= result.covered_from:
            return None
        return VideoWindow(
            camera_id=camera_id,
            t_from=result.covered_from,
            t_to=result.covered_to,
            requested_s=max((t_to - t_from).total_seconds(), 0.0),
            archive_id=(
                result.source_ref.archive_id
                if result.source_ref and result.source_ref.archive_id
                else request_id
            ),
            source_uri=self._source_uri(result),
        )

    def frames(self, window: VideoWindow, sample_fps: float):
        if window.source_uri is None:
            raise DecodeError("CCTV Archive window has no source URI")
        capture = cv2.VideoCapture(window.source_uri)
        if not capture.isOpened():
            raise DecodeError("cannot open CCTV Archive segment")
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0 or count <= 0:
            capture.release()
            raise DecodeError("CCTV Archive segment has no fps or frames")
        step = max(int(round(fps / sample_fps)), 1)
        try:
            for index in range(0, count, step):
                capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, frame = capture.read()
                if not ok:
                    raise DecodeError(f"CCTV Archive frame {index} failed")
                observed_at = window.t_from + timedelta(seconds=index / fps)
                if observed_at > window.t_to:
                    break
                yield observed_at, frame
        finally:
            capture.release()

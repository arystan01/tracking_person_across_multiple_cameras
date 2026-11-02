from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

import cv2

from .config import CameraSource


class DecodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoWindow:
    camera_id: str
    t_from: datetime
    t_to: datetime
    requested_s: float
    archive_id: str


class LocalVideoArchive:
    def __init__(self, cameras: dict[str, CameraSource]) -> None:
        self.cameras = cameras
        self._meta: dict[str, tuple[float, int]] = {}

    def has(self, camera_id: str) -> bool:
        return camera_id in self.cameras

    def _video_meta(self, camera_id: str) -> tuple[float, int]:
        if camera_id not in self._meta:
            cap = cv2.VideoCapture(self.cameras[camera_id].path)
            if not cap.isOpened():
                raise DecodeError(f"cannot open video for {camera_id}")
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            cap.release()
            if fps <= 0 or count <= 0:
                raise DecodeError(f"video for {camera_id} has no fps or frames")
            self._meta[camera_id] = (fps, count)
        return self._meta[camera_id]

    def window(self, camera_id: str, t_from: datetime, t_to: datetime) -> VideoWindow | None:
        source = self.cameras[camera_id]
        fps, count = self._video_meta(camera_id)
        video_end = source.start + timedelta(seconds=count / fps)
        start = max(t_from, source.start)
        end = min(t_to, video_end)
        if end <= start:
            return None
        return VideoWindow(
            camera_id=camera_id,
            t_from=start,
            t_to=end,
            requested_s=max((t_to - t_from).total_seconds(), 0.0),
            archive_id=source.archive_id or f"file:{camera_id}",
        )

    def frames(self, window: VideoWindow, sample_fps: float) -> Iterator[tuple[datetime, object]]:
        source = self.cameras[window.camera_id]
        fps, count = self._video_meta(window.camera_id)
        first = math.ceil((window.t_from - source.start).total_seconds() * fps)
        last = min(int((window.t_to - source.start).total_seconds() * fps), count - 1)
        step = max(int(round(fps / sample_fps)), 1)
        cap = cv2.VideoCapture(source.path)
        if not cap.isOpened():
            raise DecodeError(f"cannot open video for {window.camera_id}")
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, first)
            index = first
            while index <= last:
                if (index - first) % step == 0:
                    ok, frame = cap.read()
                    if not ok:
                        raise DecodeError(f"frame {index} of {window.camera_id} failed")
                    yield source.start + timedelta(seconds=index / fps), frame
                elif not cap.grab():
                    raise DecodeError(f"frame {index} of {window.camera_id} failed")
                index += 1
        finally:
            cap.release()

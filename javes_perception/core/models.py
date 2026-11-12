from __future__ import annotations

import math
from types import SimpleNamespace

import cv2
import numpy as np


def byte_track_buffer_frames(sample_fps: float, lost_buffer_s: float) -> int:
    """Convert the configured wall-clock retention into sampled frames."""
    return max(int(math.ceil(sample_fps * lost_buffer_s)), 1)


def _byte_tracker_args(
    tracker: str,
    sample_fps: float,
    lost_buffer_s: float,
) -> SimpleNamespace:
    from ultralytics.utils import YAML
    from ultralytics.utils.checks import check_yaml

    values = YAML.load(check_yaml(tracker))
    if values.get("tracker_type") != "bytetrack":
        raise ValueError("YoloByteTrack requires a ByteTrack tracker config")
    values["track_buffer"] = byte_track_buffer_frames(
        sample_fps, lost_buffer_s
    )
    return SimpleNamespace(**values)


def pick_device(name: str) -> str:
    if name != "auto":
        return name
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class YoloByteTrack:
    def __init__(
        self,
        weights: str,
        tracker: str,
        device: str,
        conf: float,
        imgsz: int,
        sample_fps: float,
        lost_buffer_s: float,
    ) -> None:
        from ultralytics import YOLO

        self.model = YOLO(weights)
        self.tracker_args = _byte_tracker_args(
            tracker, sample_fps, lost_buffer_s
        )
        self.device = device
        self.conf = conf
        self.imgsz = imgsz
        self._tracker = None

    def reset(self) -> None:
        from ultralytics.trackers.byte_tracker import BYTETracker

        self._tracker = BYTETracker(self.tracker_args)

    def detect(self, frame: np.ndarray) -> list[tuple[tuple[float, float, float, float], float]]:
        result = self.model.predict(
            frame, classes=[0], conf=0.25, device=self.device, imgsz=self.imgsz, verbose=False
        )[0]
        boxes = result.boxes
        return [
            (tuple(float(v) for v in xyxy), float(conf))
            for xyxy, conf in zip(boxes.xyxy.tolist(), boxes.conf.tolist())
        ]

    def track(self, frame: np.ndarray) -> list[tuple[int, tuple[float, float, float, float], float]]:
        if self._tracker is None:
            self.reset()
        result = self.model.predict(
            frame,
            classes=[0],
            conf=self.conf,
            device=self.device,
            imgsz=self.imgsz,
            verbose=False,
        )[0]
        boxes = result.boxes
        assert self._tracker is not None
        tracked = self._tracker.update(boxes.cpu().numpy(), frame)
        if tracked.size == 0:
            return []
        return [
            (
                int(row[4]),
                tuple(float(value) for value in row[:4]),
                float(row[5]),
            )
            for row in np.atleast_2d(tracked)
        ]


class OsnetEmbedder:
    def __init__(self, model_name: str, weights: str, device: str, flip: bool = True) -> None:
        from torchreid.reid.utils import FeatureExtractor

        self.flip = flip
        self.extractor = FeatureExtractor(
            model_name=model_name, model_path=weights, device=device, verbose=False
        )

    def __call__(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.zeros((0, 512), dtype=np.float32)
        rgb = [cv2.cvtColor(crop, cv2.COLOR_BGR2RGB) for crop in crops]
        features = self.extractor(rgb).detach().cpu().numpy().astype(np.float32)
        if self.flip:
            mirrored = [np.ascontiguousarray(crop[:, ::-1]) for crop in rgb]
            features += self.extractor(mirrored).detach().cpu().numpy().astype(np.float32)
        return normalize(features)


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)

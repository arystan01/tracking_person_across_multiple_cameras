from __future__ import annotations

import cv2
import numpy as np


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
    def __init__(self, weights: str, tracker: str, device: str, conf: float, imgsz: int) -> None:
        from ultralytics import YOLO

        self.model = YOLO(weights)
        self.tracker = tracker
        self.device = device
        self.conf = conf
        self.imgsz = imgsz
        self._fresh = True

    def reset(self) -> None:
        self._fresh = True

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
        result = self.model.track(
            frame,
            persist=not self._fresh,
            tracker=self.tracker,
            classes=[0],
            conf=self.conf,
            device=self.device,
            imgsz=self.imgsz,
            verbose=False,
        )[0]
        self._fresh = False
        boxes = result.boxes
        if boxes is None or boxes.id is None:
            return []
        return [
            (int(track_id), tuple(float(v) for v in xyxy), float(conf))
            for track_id, xyxy, conf in zip(
                boxes.id.tolist(), boxes.xyxy.tolist(), boxes.conf.tolist()
            )
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

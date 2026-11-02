from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml

from .config import CameraSource, PerceptionSettings
from .models import normalize
from .worker import PerceptionWorker

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ground_homography(cam_file: Path) -> np.ndarray:
    values = yaml.safe_load(cam_file.read_text())["projectionMatrix_3x4_w2p"]
    p = np.array(values, dtype=np.float64).reshape(3, 4)
    return np.linalg.inv(p[:, [0, 1, 3]])


def to_floor(h_inv: np.ndarray, xyxy) -> tuple[float, float]:
    x1, _, x2, y2 = xyxy
    point = h_inv @ np.array([(x1 + x2) / 2, y2, 1.0])
    return float(point[0] / point[2]), float(point[1] / point[2])


def run_camera(worker: PerceptionWorker, camera_id: str, seconds: float, h_inv: np.ndarray) -> list[dict]:
    window = worker.archive.window(camera_id, T0, T0 + timedelta(seconds=seconds))
    tracks, _ = worker._track(window)
    out = []
    for track in tracks.values():
        if track.frames < worker.settings.min_track_frames or not track.features:
            continue
        best = int(np.argmax(track.weights))
        out.append({
            "camera": camera_id,
            "embedding": worker._track_embedding(track),
            "photo": track.features[best],
            "floor": {round((ts - T0).total_seconds(), 2): to_floor(h_inv, xyxy) for ts, xyxy in track.path},
        })
    return out


def same_person(a: dict, b: dict, radius: float, min_common: int) -> bool | None:
    common = sorted(set(a["floor"]) & set(b["floor"]))
    if len(common) < min_common:
        return None
    dist = [np.hypot(a["floor"][t][0] - b["floor"][t][0], a["floor"][t][1] - b["floor"][t][1]) for t in common]
    return float(np.median(dist)) < radius


def evaluate(tracks: list[dict], radius: float, min_common: int, key: str) -> dict:
    hits, aps, pos, neg = [], [], [], []
    for i, query in enumerate(tracks):
        ranked = []
        for j, other in enumerate(tracks):
            if j == i or other["camera"] == query["camera"]:
                continue
            label = same_person(query, other, radius, min_common)
            if label is None:
                continue
            cosine = float(query[key] @ other["embedding"])
            ranked.append((cosine, label))
            (pos if label else neg).append(cosine)
        if not any(label for _, label in ranked) or all(label for _, label in ranked):
            continue
        ranked.sort(key=lambda x: -x[0])
        labels = np.array([label for _, label in ranked])
        hits.append(labels[0])
        cum = np.cumsum(labels)
        aps.append(float((cum[labels] / (np.flatnonzero(labels) + 1)).mean()))
    return {
        "queries": len(hits),
        "rank1": round(float(np.mean(hits)), 4),
        "mAP": round(float(np.mean(aps)), 4),
        "same_cos_median": round(float(np.median(pos)), 3),
        "other_cos_median": round(float(np.median(neg)), 3),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Cross-camera Re-ID check on NVIDIA MV3DT warehouse videos")
    parser.add_argument("--data", required=True, help="e.g. data/nvidia_mtmc/mtmc_4cam")
    parser.add_argument("--model", default="osnet_ain_x1_0")
    parser.add_argument("--weights", default="models/osnet_ain_x1_0_msmt17.pt")
    parser.add_argument("--detector", default="models/yolo26n.pt")
    parser.add_argument("--no-filter", action="store_true")
    parser.add_argument("--radius", type=float, default=1.5, help="same floor point, world units (feet here)")
    parser.add_argument("--min-common", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args(argv)

    data = Path(args.data)
    videos = sorted((data / "videos").glob("*.mp4"))
    cameras = {v.stem: CameraSource(path=str(v), start=T0) for v in videos}
    settings = PerceptionSettings(
        cameras=cameras,
        detector_weights=args.detector,
        reid_model=args.model,
        reid_weights=args.weights,
        crop_filter=not args.no_filter,
    )
    worker = PerceptionWorker(settings, artifacts=None)
    tracks = []
    for video in videos:
        tracks += run_camera(worker, video.stem, args.seconds, ground_homography(data / "camInfo" / f"{video.stem}.yml"))
    print(json.dumps({
        "data": data.name,
        "model": args.model,
        "detector": Path(args.detector).name,
        "crop_filter": not args.no_filter,
        "tracks": len(tracks),
        "photo_to_tracks": evaluate(tracks, args.radius, args.min_common, "photo"),
        "track_to_tracks": evaluate(tracks, args.radius, args.min_common, "embedding"),
    }))


if __name__ == "__main__":
    main()

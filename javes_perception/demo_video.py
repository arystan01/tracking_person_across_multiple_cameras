from __future__ import annotations

import argparse
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from .config import CameraSource, PerceptionSettings
from .worker import PerceptionWorker

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TILE_W, TILE_H = 960, 540
GREEN = (60, 220, 60)
GREY = (170, 170, 170)


def pick_target(worker, window, at_s: float):
    tracks, _ = worker._track(window)
    at = T0 + timedelta(seconds=at_s)
    alive = [t for t in tracks.values() if t.features and t.first_ts <= at <= t.last_ts and t.best_crop is not None]
    if not alive:
        raise SystemExit("no person on the query camera at that time")
    target = max(alive, key=lambda t: t.best_quality)
    return target.best_crop, worker._track_embedding(target)


def scored_tracks(worker, window, gallery: np.ndarray):
    tracks, _ = worker._track(window)
    out = []
    for track in tracks.values():
        if track.frames < worker.settings.min_track_frames or not track.features:
            continue
        cosine = float(np.max(gallery @ worker._track_embedding(track)))
        out.append({"score": worker._score(cosine), "boxes": {ts: xyxy for ts, xyxy in track.path}})
    return out


def draw_tile(frame, people, ts, name, threshold):
    scale_x, scale_y = TILE_W / frame.shape[1], TILE_H / frame.shape[0]
    tile = cv2.resize(frame, (TILE_W, TILE_H))
    best = max((p for p in people if ts in p["boxes"]), key=lambda p: p["score"], default=None)
    found = False
    for person in people:
        if ts not in person["boxes"]:
            continue
        x1, y1, x2, y2 = person["boxes"][ts]
        p1 = (int(x1 * scale_x), int(y1 * scale_y))
        p2 = (int(x2 * scale_x), int(y2 * scale_y))
        if person is best and person["score"] >= threshold:
            found = True
            cv2.rectangle(tile, p1, p2, GREEN, 3)
            label = f"TARGET {person['score']:.2f}"
            cv2.rectangle(tile, (p1[0], p1[1] - 24), (p1[0] + 150, p1[1]), GREEN, -1)
            cv2.putText(tile, label, (p1[0] + 4, p1[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
        else:
            cv2.rectangle(tile, p1, p2, GREY, 1)
            cv2.putText(tile, f"{person['score']:.2f}", (p1[0], p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, GREY, 1)
    color = GREEN if found else (255, 255, 255)
    cv2.rectangle(tile, (0, 0), (TILE_W, 36), (0, 0, 0), -1)
    status = "target found" if found else "searching"
    cv2.putText(tile, f"{name}  |  {status}", (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.rectangle(tile, (0, 0), (TILE_W - 1, TILE_H - 1), color if found else (40, 40, 40), 4 if found else 1)
    return tile


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Demo video: find one person on all cameras")
    parser.add_argument("--data", default="data/nvidia_mtmc/mtmc_4cam")
    parser.add_argument("--query-camera", default="Warehouse_Synthetic_Cam002")
    parser.add_argument("--query-time", type=float, default=20.0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--out", default="perception_out/demo_search.mp4")
    args = parser.parse_args(argv)

    data = Path(args.data)
    videos = sorted((data / "videos").glob("*.mp4"))[:4]
    cameras = {v.stem: CameraSource(path=str(v), start=T0) for v in videos}
    worker = PerceptionWorker(PerceptionSettings(cameras=cameras, sample_fps=args.fps), artifacts=None)
    end = T0 + timedelta(seconds=args.seconds)
    windows = {c: worker.archive.window(c, T0, end) for c in cameras}

    print("picking the target on", args.query_camera)
    reference, embedding = pick_target(worker, windows[args.query_camera], args.query_time)
    gallery = embedding[None, :]
    people = {}
    for camera in cameras:
        print("searching", camera)
        people[camera] = scored_tracks(worker, windows[camera], gallery)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (TILE_W * 2, TILE_H * 2 + 120))
    ref = cv2.resize(reference, (int(110 * reference.shape[1] / reference.shape[0]), 110))
    streams = [worker.archive.frames(windows[c], args.fps) for c in cameras]
    names = [c.replace("Warehouse_Synthetic_", "") for c in cameras]
    for frames in zip(*streams):
        tiles = [draw_tile(frame, people[c], ts, n, args.threshold) for (ts, frame), c, n in zip(frames, cameras, names)]
        canvas = np.zeros((TILE_H * 2 + 120, TILE_W * 2, 3), np.uint8)
        canvas[5 : 5 + ref.shape[0], 10 : 10 + ref.shape[1]] = ref
        cv2.putText(canvas, "Reference photo (taken from " + args.query_camera.replace("Warehouse_Synthetic_", "") + ")",
                    (30 + ref.shape[1], 45), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        cv2.putText(canvas, "JAVES-AI Perception: YOLO26 + ByteTrack + OSNet-AIN   t = "
                    f"{(frames[0][0] - T0).total_seconds():5.1f} s",
                    (30 + ref.shape[1], 90), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
        canvas[120:120 + TILE_H, :TILE_W] = tiles[0]
        canvas[120:120 + TILE_H, TILE_W:] = tiles[1]
        canvas[120 + TILE_H:, :TILE_W] = tiles[2]
        canvas[120 + TILE_H:, TILE_W:] = tiles[3]
        writer.write(canvas)
    writer.release()

    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-crf", "23", str(out)], check=True)
        raw.unlink()
    else:
        raw.rename(out)
    print("saved", out)


if __name__ == "__main__":
    main()

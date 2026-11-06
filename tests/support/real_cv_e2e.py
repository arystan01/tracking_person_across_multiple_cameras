"""Reusable helpers for inspectable, opt-in real-CV end-to-end tests."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest


class SyncASGIClient:
    """Synchronous facade over an ASGI app without bypassing HTTP contracts."""

    def __init__(self, app, *, base_url: str) -> None:
        self.app = app
        self.base_url = base_url

    def request(self, method: str, url: str, **kwargs):
        import httpx

        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self.base_url,
            ) as client:
                return await client.request(method, url, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(send())).result(timeout=60)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


def require_real_cv() -> None:
    if os.environ.get("JAVES_RUN_REAL_CV") != "1":
        pytest.skip("set JAVES_RUN_REAL_CV=1 in the real-CV environment")
    missing = [
        module
        for module in ("cv2", "ultralytics", "torchreid")
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        pytest.skip(f"missing real-CV dependencies: {', '.join(missing)}")


def nvidia_pixel_to_map_homographies(
    fixture: Path,
    camera_ids: list[str],
) -> dict[str, list[list[float]]]:
    """Compose NVIDIA world/camera transforms into pixel-to-map matrices."""

    import numpy as np
    import yaml

    transforms = yaml.safe_load((fixture / "transforms.yml").read_text())
    world_to_map = np.asarray(transforms["T_ov2px"], dtype=float).reshape(3, 3)
    result: dict[str, list[list[float]]] = {}
    for camera in camera_ids:
        raw = yaml.safe_load(
            (fixture / "camInfo" / f"Warehouse_Synthetic_{camera}.yml").read_text()
        )
        projection = np.asarray(
            raw["projectionMatrix_3x4_w2p"], dtype=float
        ).reshape(3, 4)
        ground_plane_to_pixel = projection[:, [0, 1, 3]]
        pixel_to_map = world_to_map @ np.linalg.inv(ground_plane_to_pixel)
        pixel_to_map /= pixel_to_map[2, 2]
        result[camera] = pixel_to_map.tolist()
    return result


def project_pixel(
    homography: list[list[float]],
    x: float,
    y: float,
) -> tuple[float, float]:
    import numpy as np

    projected = np.asarray(homography, dtype=float) @ np.asarray([x, y, 1.0])
    projected /= projected[2]
    return float(projected[0]), float(projected[1])


def clip_video_window(
    source: Path,
    destination: Path,
    *,
    start_s: float,
    duration_s: float,
) -> None:
    """Make a local video behave like an archive returning an exact window."""

    import cv2

    capture = cv2.VideoCapture(str(source))
    assert capture.isOpened(), f"cannot open {source}"
    fps = capture.get(cv2.CAP_PROP_FPS)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    assert fps > 0 and width > 0 and height > 0
    first_frame = max(int(round(start_s * fps)), 0)
    frame_count = max(int(round(duration_s * fps)), 1)
    capture.set(cv2.CAP_PROP_POS_FRAMES, first_frame)
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    assert writer.isOpened(), f"cannot create {destination}"
    try:
        for _ in range(frame_count):
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
    finally:
        writer.release()
        capture.release()
    assert destination.is_file() and destination.stat().st_size > 0


def video_duration_s(path: Path) -> float:
    import cv2

    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened(), f"cannot open {path}"
    fps = capture.get(cv2.CAP_PROP_FPS)
    frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    capture.release()
    assert fps > 0 and frames > 0
    return float(frames / fps)


@dataclass(frozen=True)
class ReportVideo:
    camera_id: str
    path: Path
    role: str
    offset_s: float = 0.0
    note: str = ""

    def manifest(self) -> dict[str, Any]:
        data = asdict(self)
        data["path"] = f"videos/{self.camera_id}.mp4"
        return data


def _replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.resolve())


def _report_page(report: dict, videos: list[dict], timeline: list[dict]) -> str:
    summary = html.escape(json.dumps(report, indent=2))
    timeline_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('time_s', '')))}</td>"
        f"<td>{html.escape(str(item.get('camera', '')))}</td>"
        f"<td>{html.escape(str(item.get('event', '')))}</td>"
        f"<td>{html.escape(str(item.get('details', '')))}</td>"
        "</tr>"
        for item in timeline
    )
    video_cards = "".join(
        "<article class='video-card'>"
        f"<h3>{html.escape(item['camera_id'])}</h3>"
        f"<p><strong>{html.escape(item['role'])}</strong>"
        f" · {html.escape(item.get('note', ''))}</p>"
        f"<video preload='metadata' muted playsinline data-offset='{item['offset_s']}' "
        f"src='{html.escape(item['path'])}'></video>"
        "</article>"
        for item in videos
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JAVES real-CV E2E</title>
<style>
body{{font:14px system-ui;margin:0;background:#f1f5f9;color:#0f172a}}
header,main{{max-width:1500px;margin:auto;padding:18px}}
.controls{{position:sticky;top:0;z-index:3;background:#fff;padding:12px;border:1px solid #cbd5e1;border-radius:10px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.controls input[type=range]{{min-width:360px;flex:1}}button,select{{padding:7px 12px}}
.videos{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px;margin-top:16px}}
.video-card{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:12px}}
.video-card h3,.video-card p{{margin:0 0 8px}}video{{width:100%;background:#020617}}
.map{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:12px;margin-top:16px}}
.map object{{width:100%;min-height:680px}}table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{text-align:left;border:1px solid #cbd5e1;padding:7px}}pre{{background:#fff;padding:16px;overflow:auto}}
</style></head><body><header><h1>JAVES real-CV E2E</h1>
<div class="controls"><button id="play">Play</button><button id="pause">Pause</button>
<label>Time <input id="seek" type="range" min="0" max="1" step="0.05" value="0"></label>
<output id="clock">0.00 s</output><label>Speed <select id="speed"><option>0.5</option><option selected>1</option><option>2</option></select></label></div>
</header><main><section class="videos">{video_cards}</section>
<section class="map"><h2>Final Routing map</h2><object data="route.svg" type="image/svg+xml"></object></section>
<h2>Timeline</h2><table><thead><tr><th>Seconds</th><th>Camera</th><th>Event</th><th>Details</th></tr></thead><tbody>{timeline_rows}</tbody></table>
<h2>Run report</h2><pre>{summary}</pre></main>
<script>
const videos=[...document.querySelectorAll('video')], seek=document.querySelector('#seek'), clock=document.querySelector('#clock');
let masterTime=0, seeking=false;
const offset=v=>Number(v.dataset.offset||0), localTime=v=>Math.max(masterTime-offset(v),0);
function duration(){{return Math.max(0,...videos.map(v=>(Number.isFinite(v.duration)?v.duration:0)+offset(v)))}}
function sync(force=false){{for(const v of videos){{const wanted=Math.min(localTime(v),Number.isFinite(v.duration)?v.duration:localTime(v));if(force||Math.abs(v.currentTime-wanted)>0.15)v.currentTime=wanted;}}clock.value=masterTime.toFixed(2)+' s';seek.value=masterTime;}}
for(const v of videos){{v.addEventListener('loadedmetadata',()=>{{seek.max=duration();sync(true)}});v.addEventListener('timeupdate',()=>{{if(!seeking&&v===videos[0]){{masterTime=v.currentTime+offset(v);sync(false)}}}})}}
document.querySelector('#play').onclick=()=>{{sync(true);for(const v of videos)v.play()}};
document.querySelector('#pause').onclick=()=>{{for(const v of videos)v.pause()}};
seek.oninput=()=>{{seeking=true;masterTime=Number(seek.value);sync(true)}};seek.onchange=()=>{{seeking=false}};
document.querySelector('#speed').onchange=e=>{{for(const v of videos)v.playbackRate=Number(e.target.value)}};
</script></body></html>"""


def publish_inspectable_report(
    output_root: Path,
    run_root: Path,
    *,
    route_svg: bytes,
    case_view: dict,
    probe_results: list[dict],
    report: dict,
    videos: list[ReportVideo],
    timeline: list[dict],
    extra_files: dict[str, bytes] | None = None,
) -> None:
    """Publish immutable run output plus stable aliases for the latest run."""

    output_root.mkdir(parents=True, exist_ok=True)
    video_manifest = [item.manifest() for item in videos]
    files = {
        "route.svg": route_svg,
        "case-view.json": json.dumps(case_view, indent=2).encode(),
        "probe-results.json": json.dumps(probe_results, indent=2).encode(),
        "report.json": json.dumps(report, indent=2).encode(),
        "video-manifest.json": json.dumps(video_manifest, indent=2).encode(),
        **(extra_files or {}),
    }
    for name, data in files.items():
        (run_root / name).write_bytes(data)
        shutil.copyfile(run_root / name, output_root / name)
    for item in videos:
        _replace_symlink(run_root / "videos" / f"{item.camera_id}.mp4", item.path)
        _replace_symlink(output_root / "videos" / f"{item.camera_id}.mp4", item.path)
    page = _report_page(report, video_manifest, timeline)
    (run_root / "index.html").write_text(page, encoding="utf-8")
    (output_root / "index.html").write_text(page, encoding="utf-8")
    (output_root / "LATEST.txt").write_text(
        str(run_root.resolve()) + "\n", encoding="utf-8"
    )

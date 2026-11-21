"""FFmpeg media operations. Outputs intentionally contain video only."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import tempfile
import threading
import time


class MediaError(RuntimeError):
    pass


class MediaCancelled(MediaError):
    pass


class FFmpegMedia:
    def __init__(self, *, timeout_s: float = 600):
        self.timeout_s = timeout_s
        self.cancel_event = threading.Event()

    def reset_cancel(self):
        self.cancel_event.clear()

    def cancel(self):
        self.cancel_event.set()

    def _run(self, args):
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        try:
            while True:
                if self.cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise MediaCancelled('media operation cancelled')
                if time.monotonic() - started > self.timeout_s:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise MediaError('media command timed out')
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        if process.returncode:
            # FFmpeg output can contain source paths and vendor diagnostics. Keep
            # the full details in neither callbacks nor application exceptions.
            raise MediaError('media command failed')
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def probe_info(self, path: Path):
        data = json.loads(self._run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]).stdout)
        video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
        if video is None:
            raise MediaError('source has no video stream')
        duration = float(video.get('duration', data['format'].get('duration', 0)))
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError('invalid video duration')
        raw_rate = video.get('avg_frame_rate') or video.get('r_frame_rate') or '0/1'
        numerator, denominator = (float(part) for part in raw_rate.split('/', 1))
        fps = numerator / denominator if denominator else 0.0
        if not math.isfinite(fps) or fps <= 0:
            raise MediaError('invalid video frame rate')
        return {'duration': duration, 'width': int(video['width']), 'height': int(video['height']), 'fps': fps}

    def probe(self, path: Path):
        info = self.probe_info(path)
        return info['duration'], info['width'], info['height']

    def cut(self, source: Path, output: Path, start: float, duration: float):
        source_duration, _, _ = self.probe(source)
        if not all(math.isfinite(x) for x in (start, duration)) or start < 0 or duration <= 0:
            raise ValueError('invalid clip window')
        if start + duration > source_duration + 0.05:
            raise MediaError('source video does not cover requested time range')
        self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(source), '-ss', str(start), '-t', str(duration),
                   '-map', '0:v:0', '-an', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1',
                   '-map_metadata', '-1', '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
                   '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)])
        info = self.probe_info(output)
        measured = info['duration']
        if abs(measured - duration) > max(1 / info['fps'], 0.05):
            raise MediaError('output duration differs from requested duration')
        return measured

    def merge(self, sources: list[Path], output: Path):
        first = self.probe_info(sources[0])
        width, height, fps = first['width'], first['height'], first['fps']
        width, height = width // 2 * 2, height // 2 * 2
        # Normalize separately to avoid opening all source files at once.
        with tempfile.TemporaryDirectory(dir=output.parent) as folder:
            folder = Path(folder)
            for i, source in enumerate(sources):
                self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(source), '-map', '0:v:0', '-an',
                           '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.8f}',
                           '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', str(folder/f'{i}.mp4')])
            listing = folder/'inputs.txt'
            listing.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(sources))))
            self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-f', 'concat', '-safe', '1', '-i', str(listing),
                       '-map_metadata', '-1', '-c', 'copy', '-movflags', '+faststart', str(output)])
        return self.probe(output)[0]

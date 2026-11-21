"""FFmpeg media operations. Outputs intentionally contain video only."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import tempfile


class MediaError(RuntimeError):
    pass


class FFmpegMedia:
    def __init__(self, *, timeout_s: float = 600):
        self.timeout_s = timeout_s

    def _run(self, args):
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.CalledProcessError as exc:
            raise MediaError('media command failed: ' + exc.stderr[-1500:]) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError('media command timed out') from exc

    def probe(self, path: Path):
        data = json.loads(self._run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]).stdout)
        video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
        if video is None:
            raise MediaError('source has no video stream')
        duration = float(video.get('duration', data['format'].get('duration', 0)))
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError('invalid video duration')
        return duration, int(video['width']), int(video['height'])

    def cut(self, source: Path, output: Path, start: float, duration: float):
        source_duration, _, _ = self.probe(source)
        if not all(math.isfinite(x) for x in (start, duration)) or start < 0 or duration <= 0:
            raise ValueError('invalid clip window')
        if start + duration > source_duration + 0.05:
            raise MediaError('source video does not cover requested time range')
        self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(source), '-ss', str(start), '-t', str(duration),
                   '-map', '0:v:0', '-an', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1',
                   '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)])
        measured = self.probe(output)[0]
        if abs(measured - duration) > 0.2:
            raise MediaError('output duration differs from requested duration')
        return measured

    def merge(self, sources: list[Path], output: Path):
        _, width, height = self.probe(sources[0])
        width, height = width // 2 * 2, height // 2 * 2
        # Normalize separately to avoid opening all source files at once.
        with tempfile.TemporaryDirectory(dir=output.parent) as folder:
            folder = Path(folder)
            for i, source in enumerate(sources):
                self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-i', str(source), '-map', '0:v:0', '-an',
                           '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25',
                           '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', str(folder/f'{i}.mp4')])
            listing = folder/'inputs.txt'
            listing.write_text(''.join(f"file '{i}.mp4'\n" for i in range(len(sources))))
            self._run(['ffmpeg', '-v', 'error', '-nostdin', '-n', '-f', 'concat', '-safe', '1', '-i', str(listing),
                       '-c', 'copy', '-movflags', '+faststart', str(output)])
        return self.probe(output)[0]

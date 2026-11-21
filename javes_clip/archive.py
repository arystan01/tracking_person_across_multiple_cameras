"""Read-only archive access using the same public endpoint as Perception."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from javes_contracts import VideoSegmentRequest, VideoSegmentResult
from javes_contracts.acceptance import check_video_segment_result
from javes_telemetry import operation


class SourceUnavailable(RuntimeError):
    pass


class HttpClipArchive:
    """The first frame of a resolved segment is at covered_from (UTC).

    Partial windows are reported unavailable rather than silently shortening
    evidence. Padding is already included in CutClipCommand.t_from/t_to.
    """

    MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024

    def __init__(self, base_url: str, token: str, *, archive_root: str | Path, client=None):
        self.base_url = base_url.rstrip('/')
        self.root = Path(archive_root).resolve()
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.headers = {'authorization': f'Bearer {token}'}

    @contextmanager
    def segment(self, command, workdir: Path):
        request = VideoSegmentRequest(request_id=command.clip_request_id, camera_id=command.camera_id,
                                      t_from=command.t_from, t_to=command.t_to)
        with operation('clip.archive.resolve', {'component': 'clip', 'operation': 'resolve'}):
            response = self.client.post(self.base_url+'/v1/video-segments', headers=self.headers, json=request.model_dump(mode='json'))
        response.raise_for_status()
        result = VideoSegmentResult.model_validate(response.json())
        check_video_segment_result(request, result)
        if (result.availability != 'AVAILABLE' or result.covered_from is None or result.covered_to is None
                or result.covered_from > command.t_from or result.covered_to < command.t_to):
            raise SourceUnavailable('archive does not fully cover requested window')
        ref = result.source_ref
        if ref is None:
            raise SourceUnavailable('archive has no source reference')
        if command.source_ref.archive_id and ref.archive_id != command.source_ref.archive_id:
            raise ValueError('archive source identity mismatch')
        downloaded: Path | None = None
        if ref.uri:
            parsed = urlparse(ref.uri)
            if parsed.scheme == 'file' and parsed.netloc in {'', 'localhost'}:
                source = Path(unquote(parsed.path)).resolve()
                if not source.is_relative_to(self.root):
                    raise ValueError('archive file URI escapes root')
            elif parsed.scheme in {'http', 'https'}:
                source = workdir/'source.mp4'
                partial = workdir/'.source.mp4.partial'
                size = 0
                digest = hashlib.sha256()
                # Do not forward the archive API bearer token to media hosts.
                try:
                    with operation(
                        'clip.archive.download',
                        {'component': 'clip', 'operation': 'download'},
                    ), self.client.stream(
                        'GET', ref.uri, headers={}, follow_redirects=False
                    ) as download:
                        download.raise_for_status()
                        declared = download.headers.get('content-length')
                        if declared and int(declared) > self.MAX_DOWNLOAD_BYTES:
                            raise ValueError('archive media exceeds download limit')
                        with partial.open('xb') as stream:
                            for chunk in download.iter_bytes():
                                size += len(chunk)
                                if size > self.MAX_DOWNLOAD_BYTES:
                                    raise ValueError('archive media exceeds download limit')
                                digest.update(chunk)
                                stream.write(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                    if ref.sha256 and digest.hexdigest() != ref.sha256:
                        raise ValueError('archive source checksum mismatch')
                    os.replace(partial, source)
                    downloaded = source
                finally:
                    partial.unlink(missing_ok=True)
            else:
                raise ValueError('unsupported archive media URI')
        elif ref.object_key:
            source = (self.root/ref.object_key).resolve()
            if not source.is_relative_to(self.root):
                raise ValueError('archive object key escapes root')
        else:
            raise SourceUnavailable('archive result needs URI or shared object key')
        if not source.is_file():
            raise SourceUnavailable('archive source is missing')
        if ref.sha256 and downloaded is None:
            from .storage import checksum
            if checksum(source) != ref.sha256:
                raise ValueError('archive source checksum mismatch')
        try:
            yield source, result.source_offset_s + (command.t_from-result.covered_from).total_seconds()
        finally:
            if downloaded is not None:
                downloaded.unlink(missing_ok=True)

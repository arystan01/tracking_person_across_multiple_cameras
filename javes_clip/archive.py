"""Read-only archive access using the same public endpoint as Perception."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from javes_contracts import VideoSegmentRequest, VideoSegmentResult
from javes_contracts.acceptance import check_video_segment_result


class SourceUnavailable(RuntimeError):
    pass


class HttpClipArchive:
    """The first frame of a resolved segment is at covered_from (UTC).

    Partial windows are reported unavailable rather than silently shortening
    evidence. Padding is already included in CutClipCommand.t_from/t_to.
    """

    def __init__(self, base_url: str, token: str, *, artifact_root: str | Path, client=None):
        self.base_url = base_url.rstrip('/')
        self.root = Path(artifact_root).resolve()
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.headers = {'authorization': f'Bearer {token}'}

    @contextmanager
    def segment(self, command, workdir: Path):
        request = VideoSegmentRequest(request_id=command.clip_request_id, camera_id=command.camera_id,
                                      t_from=command.t_from, t_to=command.t_to)
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
        if ref.uri:
            parsed = urlparse(ref.uri)
            if parsed.scheme == 'file' and parsed.netloc in {'', 'localhost'}:
                source = Path(unquote(parsed.path))
            elif parsed.scheme in {'http', 'https'}:
                source = workdir/'source.mp4'
                # Do not forward the archive API bearer token to media hosts.
                with self.client.stream('GET', ref.uri) as download:
                    download.raise_for_status()
                    with source.open('wb') as stream:
                        for chunk in download.iter_bytes():
                            stream.write(chunk)
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
        if ref.sha256:
            from .storage import checksum
            if checksum(source) != ref.sha256:
                raise ValueError('archive source checksum mismatch')
        yield source, (command.t_from-result.covered_from).total_seconds()

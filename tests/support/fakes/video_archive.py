"""Deterministic fake for the Video Archive contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from javes_contracts import (
    ArtifactRef,
    VideoAvailability,
    VideoSegmentRequest,
    VideoSegmentResult,
)


@dataclass(frozen=True)
class ArchiveWindow:
    camera_id: str
    t_from: datetime
    t_to: datetime
    source_ref: ArtifactRef


class FakeVideoArchive:
    def __init__(self, windows: list[ArchiveWindow] | None = None) -> None:
        for item in windows or []:
            if (
                item.t_from.tzinfo is None
                or item.t_from.utcoffset() is None
                or item.t_to.tzinfo is None
                or item.t_to.utcoffset() is None
            ):
                raise ValueError("archive window timestamps must be timezone-aware")
            if item.t_to < item.t_from:
                raise ValueError("archive window end must not precede start")
        self.windows = sorted(
            windows or [], key=lambda item: (item.camera_id, item.t_from, item.t_to)
        )

    def get_segment(
        self, request: VideoSegmentRequest, *, now: datetime
    ) -> VideoSegmentResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        requested_to = now if request.t_to == "OPEN" else request.t_to
        duration = max((requested_to - request.t_from).total_seconds(), 0.0)
        matching = [
            item
            for item in self.windows
            if item.camera_id == request.camera_id
            and item.t_to > request.t_from
            and item.t_from < requested_to
        ]
        if not matching or duration <= 0:
            return VideoSegmentResult(
                request_id=request.request_id,
                camera_id=request.camera_id,
                availability=VideoAvailability.UNAVAILABLE,
                requested_from=request.t_from,
                requested_to=request.t_to,
                coverage_ratio=0.0,
                error_code="VIDEO_UNAVAILABLE",
            )
        intersections = sorted(
            (
                max(request.t_from, item.t_from),
                min(requested_to, item.t_to),
            )
            for item in matching
        )
        merged: list[tuple[datetime, datetime]] = []
        for start, end in intersections:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        covered_from = merged[0][0]
        covered_to = merged[-1][1]
        covered_s = sum((end - start).total_seconds() for start, end in merged)
        ratio = min(covered_s / duration, 1.0)
        return VideoSegmentResult(
            request_id=request.request_id,
            camera_id=request.camera_id,
            availability=(
                VideoAvailability.AVAILABLE
                if ratio >= 1.0
                else VideoAvailability.PARTIAL
            ),
            requested_from=request.t_from,
            requested_to=request.t_to,
            covered_from=covered_from,
            covered_to=covered_to,
            coverage_ratio=ratio,
            source_ref=matching[0].source_ref,
        )

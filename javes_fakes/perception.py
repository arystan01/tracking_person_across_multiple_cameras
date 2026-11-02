"""Deterministic contract fake for the future Perception worker."""

from __future__ import annotations

import hashlib
from datetime import datetime

from routing.contracts import Candidate, ProbeResult, ProbeStatus, Tracklet

from .contracts import (
    FakeObservation,
    PerceptionProbeJob,
    TargetGalleryCommand,
    TargetGalleryResult,
    aware_utc,
)


def _stable_id(prefix: str, *parts: object) -> str:
    source = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


class FakePerceptionWorker:
    """In-memory worker with finite and OPEN probe semantics.

    The fake contains no CV model. Its observations are explicit fixtures and
    its output is deterministic for identical job, time, and configuration.
    """

    def __init__(
        self,
        observations: list[FakeObservation] | None = None,
        *,
        unavailable_cameras: set[str] | None = None,
        forced_status_by_probe: dict[str, ProbeStatus] | None = None,
        coverage_by_camera: dict[str, float] | None = None,
        calibration_version: str = "fake-cal-v1",
    ) -> None:
        self.observations = sorted(
            observations or [],
            key=lambda item: (item.camera_id, item.t_start, item.observation_id),
        )
        self.unavailable_cameras = set(unavailable_cameras or set())
        self.forced_status_by_probe = dict(forced_status_by_probe or {})
        self.coverage_by_camera = dict(coverage_by_camera or {})
        self.calibration_version = calibration_version
        self._gallery_sources: dict[str, list[str]] = {}
        self._gallery_commands: dict[str, tuple[str, TargetGalleryResult]] = {}

    def update_gallery(self, command: TargetGalleryCommand) -> TargetGalleryResult:
        fingerprint = command.model_dump_json()
        previous = self._gallery_commands.get(command.command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same gallery command_id used with different payload")
            return previous[1].model_copy(deep=True)

        source = (
            command.image_ref.identity()
            if command.image_ref is not None
            else command.tracklet_id or ""
        )
        sources = self._gallery_sources.setdefault(command.case_id, [])
        if source not in sources:
            sources.append(source)
            sources.sort()
        result = TargetGalleryResult(
            command_id=command.command_id,
            case_id=command.case_id,
            gallery_ref=_stable_id(
                "gallery", command.case_id, command.model_version, *sources
            ),
            source_count=len(sources),
            model_version=command.model_version,
        )
        self._gallery_commands[command.command_id] = (fingerprint, result)
        return result.model_copy(deep=True)

    def process_probe(
        self,
        job: PerceptionProbeJob,
        *,
        now: datetime,
        close_stream: bool = False,
    ) -> ProbeResult:
        now = aware_utc(now)  # type: ignore[assignment]
        probe = job.probe
        status = self.forced_status_by_probe.get(probe.probe_id)
        if probe.camera in self.unavailable_cameras:
            status = ProbeStatus.UNAVAILABLE
        if status is not None and status != ProbeStatus.DONE:
            return ProbeResult(
                probe_id=probe.probe_id,
                status=status,
                coverage_ratio=0.0,
                processed_duration_s=0.0,
                n_tracklets_total=0,
                stream_closed=close_stream,
                error_code=f"FAKE_{status.value}",
            )

        end = probe.t_to or now
        if end < probe.t_from:
            end = probe.t_from
        coverage = self.coverage_by_camera.get(probe.camera, 1.0)
        coverage = min(max(coverage, 0.0), 1.0)
        overlapping = [
            item
            for item in self.observations
            if item.camera_id == probe.camera
            and item.t_start <= end
            and item.t_end >= probe.t_from
        ]
        tracklets: list[Tracklet] = []
        candidates: list[Candidate] = []
        for observation in overlapping:
            tracklet_id = _stable_id(
                "trk", job.case_id, observation.observation_id
            )
            t_start = max(observation.t_start, probe.t_from)
            t_end = min(observation.t_end, end)
            is_open = probe.t_to is None and not close_stream and now < observation.t_end
            clip_ref = (
                observation.clip_ref.model_dump(mode="json")
                if observation.clip_ref is not None
                else None
            )
            tracklets.append(
                Tracklet(
                    id=tracklet_id,
                    case_id=job.case_id,
                    probe_id=probe.probe_id,
                    camera=probe.camera,
                    t_in=t_start,
                    t_out=t_end,
                    open=is_open,
                    clipped_in=t_start > observation.t_start,
                    clipped_out=t_end < observation.t_end,
                    bbox_first=observation.bbox_first,
                    bbox_last=observation.bbox_last,
                    clip_ref=clip_ref,
                )
            )
            candidates.append(
                Candidate(
                    id=_stable_id("cand", job.case_id, observation.observation_id),
                    tracklet_id=tracklet_id,
                    score=observation.appearance_score,
                    cosine=observation.cosine,
                    calibration_version=self.calibration_version,
                    association_id=observation.association_id,
                )
            )

        processed = max((end - probe.t_from).total_seconds(), 0.0) * coverage
        return ProbeResult(
            probe_id=probe.probe_id,
            status=ProbeStatus.DONE,
            tracklets=tracklets,
            candidates=candidates,
            coverage_ratio=coverage,
            processed_duration_s=processed,
            n_tracklets_total=len(overlapping),
            stream_closed=close_stream,
        )

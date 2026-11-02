"""Reusable acceptance checks for real JAVES-AI component adapters.

Pydantic validates the shape of a payload.  These checks validate correlations
between a command and its result, plus cross-update invariants that JSON Schema
cannot express.  Adapter tests should run them before a real component is
enabled in Main Infra.
"""

from __future__ import annotations

from collections.abc import Mapping

from routing.contracts import ProbeStatus, SiteGraph

from .boundary import (
    ClipLifecycleState,
    ClipProcessingStatus,
    ClipRecord,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
    PerceptionProbeJob,
    RetentionCommand,
    RetentionResult,
    TargetGalleryCommand,
    TargetGalleryResult,
    VideoAvailability,
    VideoSegmentRequest,
    VideoSegmentResult,
)
from routing.contracts import ProbeResult


class ContractViolation(ValueError):
    """A well-formed payload violates a command/result boundary invariant."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def check_gallery_result(
    command: TargetGalleryCommand, result: TargetGalleryResult
) -> None:
    _require(result.command_id == command.command_id, "gallery command_id mismatch")
    _require(result.case_id == command.case_id, "gallery case_id mismatch")
    _require(result.model_version == command.model_version, "gallery model_version mismatch")


def check_probe_result(
    job: PerceptionProbeJob,
    result: ProbeResult,
    *,
    previous: ProbeResult | None = None,
) -> None:
    """Check one finite result or one update of an OPEN probe."""

    probe = job.probe
    _require(result.probe_id == probe.probe_id, "ProbeResult probe_id mismatch")
    if result.status != ProbeStatus.DONE:
        _require(not result.tracklets, "non-DONE ProbeResult must not contain tracklets")
        _require(not result.candidates, "non-DONE ProbeResult must not contain candidates")
        _require(result.coverage_ratio == 0.0, "non-DONE coverage_ratio must be zero")
    else:
        _require(
            result.processed_duration_s is not None,
            "DONE ProbeResult requires actual processed_duration_s",
        )

    tracklet_ids: set[str] = set()
    for tracklet in result.tracklets:
        _require(tracklet.id not in tracklet_ids, "duplicate tracklet_id in ProbeResult")
        tracklet_ids.add(tracklet.id)
        _require(tracklet.camera == probe.camera, "tracklet camera_id does not match probe")
        _require(tracklet.case_id in {None, job.case_id}, "tracklet case_id mismatch")
        _require(
            tracklet.probe_id in {None, probe.probe_id},
            "tracklet probe_id mismatch",
        )
        _require(tracklet.t_in >= probe.t_from, "tracklet starts before requested window")
        if probe.t_to is not None:
            _require(tracklet.t_out <= probe.t_to, "tracklet ends after requested window")

    candidate_ids: set[str] = set()
    for candidate in result.candidates:
        _require(
            candidate.tracklet_id in tracklet_ids,
            "candidate references a tracklet absent from the same result",
        )
        if candidate.id is not None:
            _require(candidate.id not in candidate_ids, "duplicate candidate_id")
            candidate_ids.add(candidate.id)
    if result.n_tracklets_total is not None:
        _require(
            result.n_tracklets_total >= len(result.tracklets),
            "n_tracklets_total is smaller than returned target tracklets",
        )
    if result.stream_closed:
        _require(probe.t_to is None, "stream_closed is valid only for an OPEN probe")

    if previous is None:
        return
    _require(probe.t_to is None, "multiple results are valid only for an OPEN probe")
    _require(previous.probe_id == result.probe_id, "OPEN update probe_id changed")
    previous_duration = previous.processed_duration_s or 0.0
    current_duration = result.processed_duration_s or 0.0
    _require(
        current_duration >= previous_duration,
        "OPEN processed_duration_s must be cumulative and monotonic",
    )
    previous_tracklets = {item.id: item for item in previous.tracklets}
    for tracklet in result.tracklets:
        old = previous_tracklets.get(tracklet.id)
        if old is None:
            continue
        _require(old.camera == tracklet.camera, "OPEN tracklet changed camera_id")
        _require(old.t_in == tracklet.t_in, "OPEN tracklet changed t_start")
        _require(tracklet.t_out >= old.t_out, "OPEN tracklet t_end moved backwards")


def check_cut_result(command: CutClipCommand, result: ClipRecord) -> None:
    _require(result.clip_request_id == command.clip_request_id, "clip request id mismatch")
    _require(result.case_id == command.case_id, "clip case_id mismatch")
    _require(result.candidate_id == command.candidate_id, "clip candidate_id mismatch")
    _require(result.tracklet_id == command.tracklet_id, "clip tracklet_id mismatch")
    _require(result.camera_id == command.camera_id, "clip camera_id mismatch")
    if result.status == ClipProcessingStatus.READY:
        _require(result.state == ClipLifecycleState.HOLD, "new READY clip must start in HOLD")
        _require(result.artifact_ref is not None, "READY clip requires artifact_ref")
        _require(bool(result.checksum), "READY clip requires checksum")
    else:
        _require(result.artifact_ref is None, "non-READY clip must not expose artifact_ref")


def check_clip_state_result(
    command: ClipStateCommand,
    result: ClipRecord,
    *,
    previous: ClipRecord | None = None,
) -> None:
    expected = {
        "HOLD": ClipLifecycleState.HOLD,
        "CONFIRM": ClipLifecycleState.CONFIRMED,
        "RELEASE": ClipLifecycleState.RELEASED,
    }[command.action]
    _require(result.case_id == command.case_id, "clip state case_id mismatch")
    _require(result.clip_id == command.clip_id, "clip state clip_id mismatch")
    _require(result.status == ClipProcessingStatus.READY, "clip state result must stay READY")
    _require(result.state == expected, "clip lifecycle state does not match command")
    if previous is not None:
        _require(result.revision > previous.revision, "clip revision must increase")


def check_export_result(
    command: ExportClipsCommand, result: ExportClipsResult
) -> None:
    _require(
        result.export_request_id == command.export_request_id,
        "export_request_id mismatch",
    )
    _require(result.case_id == command.case_id, "export case_id mismatch")
    _require(
        result.ordered_clip_ids == command.ordered_clip_ids,
        "export changed the requested clip order",
    )
    if result.status == ClipProcessingStatus.READY:
        _require(not result.missing_clip_ids, "READY export cannot have missing clips")
        _require(result.artifact_ref is not None, "READY export requires artifact_ref")
        _require(bool(result.checksum), "READY export requires checksum")


def check_retention_result(
    command: RetentionCommand, result: RetentionResult
) -> None:
    _require(result.command_id == command.command_id, "retention command_id mismatch")
    _require(result.case_id == command.case_id, "retention case_id mismatch")
    _require(result.action == command.action, "retention action mismatch")
    deleted = result.deleted_resource_ids
    preserved = result.preserved_resource_ids
    _require(len(deleted) == len(set(deleted)), "duplicate deleted resource id")
    _require(len(preserved) == len(set(preserved)), "duplicate preserved resource id")
    _require(not set(deleted) & set(preserved), "resource is both deleted and preserved")
    _require(bool(result.audit_tombstone_id), "retention result requires audit tombstone")


def check_video_segment_result(
    request: VideoSegmentRequest, result: VideoSegmentResult
) -> None:
    _require(result.request_id == request.request_id, "archive request_id mismatch")
    _require(result.camera_id == request.camera_id, "archive camera_id mismatch")
    _require(result.requested_from == request.t_from, "archive requested_from mismatch")
    _require(result.requested_to == request.t_to, "archive requested_to mismatch")
    if result.availability == VideoAvailability.AVAILABLE:
        _require(result.coverage_ratio == 1.0, "AVAILABLE segment requires full coverage")
        _require(result.source_ref is not None, "AVAILABLE segment requires source_ref")
    elif result.availability == VideoAvailability.PARTIAL:
        _require(0.0 < result.coverage_ratio < 1.0, "PARTIAL coverage must be between 0 and 1")
        _require(result.source_ref is not None, "PARTIAL segment requires source_ref")
    else:
        _require(result.coverage_ratio == 0.0, "UNAVAILABLE coverage must be zero")
        _require(result.source_ref is None, "UNAVAILABLE segment cannot have source_ref")


def check_site_snapshot(
    graph: SiteGraph,
    floor_assets: Mapping[str, bytes],
    *,
    require_all_floor_assets: bool = True,
) -> None:
    """Check the immutable graph/assets snapshot expected by Routing."""

    if graph.map is None:
        return
    floor_ids = [floor.id for floor in graph.map.floors]
    _require(len(floor_ids) == len(set(floor_ids)), "duplicate map floor id")
    if not require_all_floor_assets:
        return
    for floor in graph.map.floors:
        if floor.image is None:
            continue
        _require(
            floor.id in floor_assets or floor.image in floor_assets,
            f"missing floor asset for {floor.id!r} ({floor.image!r})",
        )


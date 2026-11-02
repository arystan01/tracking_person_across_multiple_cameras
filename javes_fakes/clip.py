"""Deterministic in-memory Clip worker fake."""

from __future__ import annotations

import hashlib

from .contracts import (
    ArtifactRef,
    ClipLifecycleState,
    ClipProcessingStatus,
    ClipRecord,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
)


def _stable_id(prefix: str, *parts: object) -> str:
    source = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


class FakeClipWorker:
    def __init__(self, unavailable_source_ids: set[str] | None = None) -> None:
        self.unavailable_source_ids = set(unavailable_source_ids or set())
        self.records: dict[str, ClipRecord] = {}
        self._cut_requests: dict[str, tuple[str, str]] = {}
        self._state_commands: dict[str, tuple[str, ClipRecord]] = {}
        self._exports: dict[str, tuple[str, ExportClipsResult]] = {}

    def cut(self, command: CutClipCommand) -> ClipRecord:
        fingerprint = command.model_dump_json()
        previous = self._cut_requests.get(command.clip_request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same clip_request_id used with different payload")
            return self.records[previous[1]].model_copy(deep=True)

        clip_id = _stable_id("clip", command.case_id, command.clip_request_id)
        unavailable = command.source_ref.identity() in self.unavailable_source_ids
        duration = max((command.t_to - command.t_from).total_seconds(), 0.0)
        artifact_ref = None
        checksum = None
        state = None
        error_code = None
        status = ClipProcessingStatus.UNAVAILABLE if unavailable else ClipProcessingStatus.READY
        if unavailable:
            error_code = "SOURCE_UNAVAILABLE"
        else:
            artifact_ref = ArtifactRef(object_key=f"cases/{command.case_id}/clips/{clip_id}.mp4")
            checksum = f"sha256:{hashlib.sha256(clip_id.encode('utf-8')).hexdigest()}"
            state = ClipLifecycleState.HOLD
        record = ClipRecord(
            clip_request_id=command.clip_request_id,
            clip_id=clip_id,
            case_id=command.case_id,
            candidate_id=command.candidate_id,
            tracklet_id=command.tracklet_id,
            status=status,
            state=state,
            camera_id=command.camera_id,
            t_start=command.t_from,
            t_end=command.t_to,
            duration_s=duration,
            artifact_ref=artifact_ref,
            checksum=checksum,
            error_code=error_code,
        )
        self.records[clip_id] = record
        self._cut_requests[command.clip_request_id] = (fingerprint, clip_id)
        return record.model_copy(deep=True)

    def change_state(self, command: ClipStateCommand) -> ClipRecord:
        fingerprint = command.model_dump_json()
        previous = self._state_commands.get(command.command_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same clip state command_id used with different payload")
            return previous[1].model_copy(deep=True)

        record = self.records.get(command.clip_id)
        if record is None or record.case_id != command.case_id:
            raise KeyError(f"unknown clip {command.clip_id!r} for case")
        if record.status != ClipProcessingStatus.READY:
            raise ValueError("cannot change lifecycle state of a non-ready clip")
        target = {
            "HOLD": ClipLifecycleState.HOLD,
            "CONFIRM": ClipLifecycleState.CONFIRMED,
            "RELEASE": ClipLifecycleState.RELEASED,
        }[command.action]
        updated = record.model_copy(
            update={"state": target, "revision": record.revision + 1}, deep=True
        )
        self.records[record.clip_id] = updated
        self._state_commands[command.command_id] = (fingerprint, updated)
        return updated.model_copy(deep=True)

    def export(self, command: ExportClipsCommand) -> ExportClipsResult:
        fingerprint = command.model_dump_json()
        previous = self._exports.get(command.export_request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("same export_request_id used with different payload")
            return previous[1].model_copy(deep=True)

        missing = [
            clip_id
            for clip_id in command.ordered_clip_ids
            if clip_id not in self.records
            or self.records[clip_id].case_id != command.case_id
            or self.records[clip_id].status != ClipProcessingStatus.READY
            or self.records[clip_id].state != ClipLifecycleState.CONFIRMED
        ]
        if missing:
            result = ExportClipsResult(
                export_request_id=command.export_request_id,
                case_id=command.case_id,
                status=ClipProcessingStatus.FAILED,
                ordered_clip_ids=list(command.ordered_clip_ids),
                missing_clip_ids=missing,
                error_code="MISSING_OR_UNCONFIRMED_CLIPS",
            )
        else:
            export_id = _stable_id(
                "export", command.case_id, command.export_request_id, *command.ordered_clip_ids
            )
            duration = sum(self.records[item].duration_s for item in command.ordered_clip_ids)
            result = ExportClipsResult(
                export_request_id=command.export_request_id,
                case_id=command.case_id,
                status=ClipProcessingStatus.READY,
                ordered_clip_ids=list(command.ordered_clip_ids),
                artifact_ref=ArtifactRef(
                    object_key=f"cases/{command.case_id}/exports/{export_id}.mp4"
                ),
                checksum=f"sha256:{hashlib.sha256(export_id.encode()).hexdigest()}",
                duration_s=duration,
            )
        self._exports[command.export_request_id] = (fingerprint, result)
        return result.model_copy(deep=True)

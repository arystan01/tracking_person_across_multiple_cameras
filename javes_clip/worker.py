"""Real clip operations against public contracts, with durable command receipts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

from javes_contracts import (
    ArtifactManifest, ArtifactRef, ClipLifecycleState, ClipPurgeCommand, ClipPurgeResult,
    ClipRecord, ClipStateCommand, CutClipCommand,
    ExportClipsCommand, ExportClipsResult,
)

from .archive import SourceUnavailable
from .media import FFmpegMedia
from .storage import ClipStore, case_prefix, checksum, fingerprint


class ClipWorker:
    def __init__(self, store: ClipStore, archive, *, media=None):
        self.store = store
        self.archive = archive
        self.media = media or FFmpegMedia()

    def _command(self, kind, request_id, command, execute, model):
        digest = fingerprint(command.model_dump(mode='json'))
        # IDs are global within each operation, matching Main's idempotency rules.
        key = f'{kind}:{request_id}'
        case_prefix(command.case_id)
        with self.store.transaction():
            previous = self.store.cached(key, digest)
            if previous is not None:
                return model.model_validate(previous)
            self.store.reserve(key, command.case_id, digest)
        # Never hold the SQLite write lock while reading the Archive or running
        # FFmpeg. The durable reservation makes the crash window observable.
        self.media.reset_cancel()
        result = execute()
        with self.store.transaction():
            previous = self.store.cached(key, digest)
            if previous is not None:
                return model.model_validate(previous)
            self.store.remember(
                key,
                digest,
                result.model_dump(mode='json'),
                case_id=command.case_id,
            )
            return result

    def cut(self, command: CutClipCommand) -> ClipRecord:
        def execute():
            clip_id = 'clip_' + fingerprint(command.model_dump(mode='json'))[:24]
            key = f'{case_prefix(command.case_id)}/clips/{clip_id}.mp4'
            path = self.store.path(key)
            duration = (command.t_to-command.t_from).total_seconds()
            if duration <= 0:
                raise ValueError('clip duration must be positive')
            record = dict(clip_request_id=command.clip_request_id, clip_id=clip_id, case_id=command.case_id,
                          candidate_id=command.candidate_id, tracklet_id=command.tracklet_id,
                          camera_id=command.camera_id, t_start=command.t_from, t_end=command.t_to, duration_s=duration)
            if not path.exists():
                with tempfile.TemporaryDirectory(dir=self.store.root, prefix='.clip-') as directory:
                    directory = Path(directory)
                    try:
                        with self.archive.segment(command, directory) as (source, offset):
                            self.media.cut(source, directory/'cut.mp4', offset, duration)
                    except SourceUnavailable:
                        result = ClipRecord(**record, status='UNAVAILABLE', error_code='SOURCE_UNAVAILABLE')
                        self.store.save_clip(result)
                        return result
                    self.store.publish(directory/'cut.mp4', key)
            measured = self.media.probe(path)[0]
            if abs(measured-duration) > 0.2:
                raise ValueError('existing output has incorrect duration')
            digest = checksum(path)
            result = ClipRecord(**{**record, 'duration_s': measured}, status='READY', state='HOLD',
                                artifact_ref=ArtifactRef(object_key=key, sha256=digest), checksum='sha256:'+digest)
            self.store.save_clip(result)
            return result
        return self._command('cut', command.clip_request_id, command, execute, ClipRecord)

    def change_state(self, command: ClipStateCommand) -> ClipRecord:
        def execute():
            raw = self.store.get_clip(command.clip_id)
            if raw is None or raw['case_id'] != command.case_id:
                raise ValueError('unknown clip for case')
            record = ClipRecord.model_validate(raw)
            if record.status != 'READY':
                raise ValueError('cannot change a non-ready clip')
            target = ClipLifecycleState({'HOLD': 'HOLD', 'CONFIRM': 'CONFIRMED', 'RELEASE': 'RELEASED'}[command.action])
            if record.state == target:
                return record
            if target == ClipLifecycleState.HOLD:
                if record.artifact_ref is None or record.artifact_ref.object_key is None:
                    raise ValueError('released clip has no restorable artifact')
                source = self.store.path(record.artifact_ref.object_key)
                if not source.is_file() or checksum(source) != record.artifact_ref.sha256:
                    raise ValueError('released clip artifact is missing or changed')
            result = record.model_copy(update={'state': target, 'revision': record.revision+1})
            self.store.save_clip(result)
            # Retention owns physical deletion. Keep the reference for registration.
            return result
        return self._command('state', command.command_id, command, execute, ClipRecord)

    def export(self, command: ExportClipsCommand) -> ExportClipsResult:
        def execute():
            sources, missing = [], []
            for clip_id in command.ordered_clip_ids:
                raw = self.store.get_clip(clip_id)
                if raw is None:
                    missing.append(clip_id)
                    continue
                record = ClipRecord.model_validate(raw)
                key = record.artifact_ref.object_key if record.artifact_ref else None
                if record.case_id != command.case_id or record.status != 'READY' or record.state != 'CONFIRMED' or not key:
                    missing.append(clip_id)
                else:
                    source = self.store.path(key)
                    if not source.is_file() or checksum(source) != record.artifact_ref.sha256:
                        missing.append(clip_id)
                    else:
                        sources.append(source)
            result = dict(export_request_id=command.export_request_id, case_id=command.case_id,
                          ordered_clip_ids=command.ordered_clip_ids)
            if missing:
                return ExportClipsResult(**result, status='FAILED', missing_clip_ids=missing,
                                         error_code='MISSING_OR_UNCONFIRMED_CLIPS')
            export_id = 'export_' + fingerprint(command.model_dump(mode='json'))[:24]
            key = f'{case_prefix(command.case_id)}/exports/{export_id}.mp4'
            path = self.store.path(key)
            if not path.exists():
                with tempfile.TemporaryDirectory(dir=self.store.root, prefix='.clip-') as directory:
                    output = Path(directory)/'merged.mp4'
                    self.media.merge(sources, output)
                    self.store.publish(output, key)
            digest = checksum(path)
            return ExportClipsResult(**result, status='READY', artifact_ref=ArtifactRef(object_key=key, sha256=digest),
                                     checksum='sha256:'+digest, duration_s=self.media.probe(path)[0])
        return self._command('export', command.export_request_id, command, execute, ExportClipsResult)

    def purge(self, command: ClipPurgeCommand) -> ClipPurgeResult:
        digest = fingerprint(command.model_dump(mode='json'))
        key = f'purge:{command.command_id}'
        case_prefix(command.case_id)
        with self.store.transaction():
            previous = self.store.cached(key, digest)
            if previous is not None:
                return ClipPurgeResult.model_validate(previous)
            self.store.purge_case(command.case_id)
            result = ClipPurgeResult(
                command_id=command.command_id,
                case_id=command.case_id,
            )
            # purge_case intentionally removes all case-scoped receipts first;
            # only this anonymised completion receipt survives.
            self.store.remember(key, digest, result.model_dump(mode='json'))
            return result

    def manifest(self, result) -> list[ArtifactManifest]:
        if result.artifact_ref is None or result.artifact_ref.object_key is None:
            return []
        key = result.artifact_ref.object_key
        path = self.store.path(key)
        digest = checksum(path)
        if result.artifact_ref.sha256 != digest:
            raise ValueError('output artifact checksum mismatch')
        is_clip = isinstance(result, ClipRecord)
        retention = 'case_14d'
        if is_clip:
            retention = {
                'HOLD': 'review_hold',
                'CONFIRMED': 'confirmed_evidence',
                'RELEASED': 'ephemeral',
            }[result.state]
        return [ArtifactManifest(object_key=key, kind='clip' if is_clip else 'export',
                                 size_bytes=path.stat().st_size, sha256=digest,
                                 created_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                                 retention_class=retention, policy_revision=getattr(result, 'revision', 1),
                                 mime_type='video/mp4')]

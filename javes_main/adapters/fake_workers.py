from __future__ import annotations

from datetime import datetime

from javes_contracts import (
    ClipRecord,
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    ExportClipsResult,
    PerceptionProbeJob,
    ProbeResult,
    RetentionCommand,
    RetentionResource,
    RetentionResult,
    TargetGalleryCommand,
    TargetGalleryResult,
)
from javes_fakes import FakeClipWorker, FakePerceptionWorker, FakeRetentionWorker


class FakePerceptionAdapter:
    def __init__(self, worker: FakePerceptionWorker) -> None:
        self.worker = worker

    def update_gallery(self, command: TargetGalleryCommand) -> TargetGalleryResult:
        return self.worker.update_gallery(command)

    def execute(self, job: PerceptionProbeJob, now: datetime) -> ProbeResult:
        return self.worker.process_probe(job, now=now)

    def cancel(self, job: PerceptionProbeJob, now: datetime) -> ProbeResult:
        return self.worker.process_probe(job, now=now, close_stream=True)


class FakeClipAdapter:
    def __init__(self, worker: FakeClipWorker) -> None:
        self.worker = worker

    def cut(self, command: CutClipCommand) -> ClipRecord:
        return self.worker.cut(command)

    def change_state(self, command: ClipStateCommand) -> ClipRecord:
        return self.worker.change_state(command)

    def export(self, command: ExportClipsCommand) -> ExportClipsResult:
        return self.worker.export(command)


class FakeRetentionAdapter:
    def __init__(self, worker: FakeRetentionWorker) -> None:
        self.worker = worker

    def register(self, resource: RetentionResource) -> None:
        self.worker.register(resource)

    def process(self, command: RetentionCommand) -> RetentionResult:
        return self.worker.process(command)

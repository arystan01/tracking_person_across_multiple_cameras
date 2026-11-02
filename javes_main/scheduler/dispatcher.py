"""Durable outbox dispatcher for Clip, Retention and cancellation commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from javes_contracts import (
    ClipStateCommand,
    CutClipCommand,
    ExportClipsCommand,
    PerceptionProbeJob,
    RetentionCommand,
    RetentionResource,
    TargetGalleryCommand,
)
from javes_main.application import CaseManager
from javes_main.domain import OutboxRecord, OutboxState, OutboxTopic
from javes_main.ports import CaseRepository, ClipPort, Clock, PerceptionPort, RetentionPort


@dataclass(frozen=True)
class DispatchReport:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0


class OutboxDispatcher:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        manager: CaseManager,
        perception: PerceptionPort,
        clip: ClipPort,
        retention: RetentionPort,
        clock: Clock,
        worker_id: str = "in-process-outbox",
        lease_s: int = 30,
        retry_backoff_s: int = 5,
    ) -> None:
        self.repository = repository
        self.manager = manager
        self.perception = perception
        self.clip = clip
        self.retention = retention
        self.clock = clock
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.retry_backoff_s = retry_backoff_s

    def run_once(self, *, limit: int = 100) -> DispatchReport:
        now = self.clock.now()
        self.repository.reclaim_expired_outbox(now=now)
        messages = self.repository.claim_outbox(
            worker_id=self.worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=self.lease_s),
            limit=limit,
        )
        delivered = retried = failed = 0
        for message in messages:
            current = self.repository.get_outbox(message.message_id)
            if (
                current.state != OutboxState.LEASED
                or current.lease_owner != self.worker_id
            ):
                continue
            if self._defer_retention_until_case_outbox_is_drained(current):
                retried += 1
                continue
            try:
                result = self._deliver(message)
                with self.repository.transaction():
                    current = self.repository.get_outbox(message.message_id)
                    if (
                        current.state != OutboxState.LEASED
                        or current.lease_owner != self.worker_id
                    ):
                        continue
                    self.manager.apply_outbox_result(message, result)
                    current.state = OutboxState.DELIVERED
                    current.lease_owner = None
                    current.lease_expires_at = None
                    current.last_error = None
                    current.updated_at = self.clock.now()
                    self.repository.save_outbox(current)
                delivered += 1
            except Exception as exc:  # worker boundary; failure becomes durable state
                current = self.repository.get_outbox(message.message_id)
                if (
                    current.state != OutboxState.LEASED
                    or current.lease_owner != self.worker_id
                ):
                    continue
                current.lease_owner = None
                current.lease_expires_at = None
                current.last_error = f"{type(exc).__name__}: {exc}"
                current.updated_at = self.clock.now()
                if current.attempt >= current.max_attempts:
                    current.state = OutboxState.FAILED
                    failed += 1
                else:
                    current.state = OutboxState.PENDING
                    current.available_at = self.clock.now() + timedelta(
                        seconds=self.retry_backoff_s * current.attempt
                    )
                    retried += 1
                self.repository.save_outbox(current)
        return DispatchReport(
            claimed=len(messages),
            delivered=delivered,
            retried=retried,
            failed=failed,
        )

    def _defer_retention_until_case_outbox_is_drained(
        self, message: OutboxRecord
    ) -> bool:
        """Preserve causal ordering before CLOSE/DELETE retention tombstones."""

        if message.topic != OutboxTopic.RETENTION_COMMAND.value:
            return False
        active = self.repository.list_outbox(
            case_id=message.case_id,
            states={OutboxState.PENDING.value, OutboxState.LEASED.value},
        )
        message_order = (message.created_at, message.message_id)
        current_action = str(message.payload.get("command", {}).get("action", ""))

        def blocks(item: OutboxRecord) -> bool:
            if item.topic != OutboxTopic.RETENTION_COMMAND.value:
                return True
            other_action = str(item.payload.get("command", {}).get("action", ""))
            if current_action == "DELETE_CASE" and other_action == "CLOSE_CASE":
                return True
            if current_action == "CLOSE_CASE" and other_action == "DELETE_CASE":
                return False
            return (item.created_at, item.message_id) < message_order

        blockers = [
            item
            for item in active
            if item.message_id != message.message_id
            and blocks(item)
        ]
        if not blockers:
            return False
        current = self.repository.get_outbox(message.message_id)
        current.state = OutboxState.PENDING
        current.attempt = max(current.attempt - 1, 0)
        current.lease_owner = None
        current.lease_expires_at = None
        blocker_times = [
            item.available_at
            for item in blockers
            if item.state == OutboxState.PENDING
            and item.available_at > self.clock.now()
        ]
        if blocker_times:
            current.available_at = max(current.available_at, min(blocker_times))
        current.updated_at = self.clock.now()
        self.repository.save_outbox(current)
        return True

    def drain(self, *, max_cycles: int = 100) -> DispatchReport:
        total = DispatchReport()
        for _ in range(max_cycles):
            report = self.run_once()
            total = DispatchReport(
                claimed=total.claimed + report.claimed,
                delivered=total.delivered + report.delivered,
                retried=total.retried + report.retried,
                failed=total.failed + report.failed,
            )
            if report.claimed == 0:
                return total
        raise RuntimeError("outbox did not become quiescent")

    def _deliver(self, message: OutboxRecord) -> Any:
        topic = OutboxTopic(message.topic)
        if topic == OutboxTopic.TARGET_GALLERY:
            return self.perception.update_gallery(
                TargetGalleryCommand.model_validate(message.payload["command"])
            )
        if topic == OutboxTopic.PERCEPTION_CANCEL:
            return self.perception.cancel(
                PerceptionProbeJob.model_validate(message.payload["job"]),
                self.clock.now(),
            )
        if topic == OutboxTopic.CLIP_CUT:
            return self.clip.cut(CutClipCommand.model_validate(message.payload["command"]))
        if topic == OutboxTopic.CLIP_STATE:
            return self.clip.change_state(
                ClipStateCommand.model_validate(message.payload["command"])
            )
        if topic == OutboxTopic.CLIP_EXPORT:
            return self.clip.export(
                ExportClipsCommand.model_validate(message.payload["command"])
            )
        if topic == OutboxTopic.RETENTION_REGISTER:
            self.retention.register(
                RetentionResource.model_validate(message.payload["resource"])
            )
            return None
        if topic == OutboxTopic.RETENTION_COMMAND:
            return self.retention.process(
                RetentionCommand.model_validate(message.payload["command"])
            )
        raise ValueError(f"unsupported outbox topic {message.topic!r}")

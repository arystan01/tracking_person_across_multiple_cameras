"""Explicit inspect/replay operations for terminal delivery failures."""

from __future__ import annotations

from typing import Literal

from javes_main.domain import JobState, OutboxState, RoutingTriggerState
from javes_main.ports import CaseRepository, Clock


DeadLetterKind = Literal["job", "outbox", "routing"]


class DeadLetterService:
    def __init__(self, repository: CaseRepository, clock: Clock) -> None:
        self.repository = repository
        self.clock = clock

    def list(self) -> list[dict]:
        entries: list[dict] = []
        for case in self.repository.list_cases():
            for item in self.repository.list_jobs(
                case.request.case_id,
                states={JobState.FAILED.value, JobState.UNAVAILABLE.value},
            ):
                entries.append(self._view("job", item.job_id, item))
        for item in self.repository.list_outbox(states={OutboxState.FAILED.value}):
            entries.append(self._view("outbox", item.message_id, item))
        for item in self.repository.list_routing_triggers(
            states={RoutingTriggerState.FAILED.value}
        ):
            entries.append(self._view("routing", item.trigger_id, item))
        return sorted(entries, key=lambda item: (item["kind"], item["id"]))

    @staticmethod
    def _view(kind: DeadLetterKind, identifier: str, item) -> dict:
        return {
            "kind": kind,
            "id": identifier,
            "case_id": item.case_id,
            "attempt": item.attempt,
            "max_attempts": item.max_attempts,
            "last_error": item.last_error,
            "updated_at": item.updated_at.isoformat(),
        }

    def replay(self, kind: DeadLetterKind, identifier: str) -> dict:
        now = self.clock.now()
        with self.repository.transaction():
            if kind == "job":
                item = self.repository.get_job(identifier)
                if item.state not in {JobState.FAILED, JobState.UNAVAILABLE}:
                    raise ValueError("only terminal jobs can be replayed")
                item.state = JobState.PENDING
                save = self.repository.save_job
            elif kind == "outbox":
                item = self.repository.get_outbox(identifier)
                if item.state != OutboxState.FAILED:
                    raise ValueError("only failed outbox messages can be replayed")
                item.state = OutboxState.PENDING
                item.available_at = now
                save = self.repository.save_outbox
            else:
                item = self.repository.get_routing_trigger(identifier)
                if item.state != RoutingTriggerState.FAILED:
                    raise ValueError("only failed Routing triggers can be replayed")
                item.state = RoutingTriggerState.PENDING
                save = self.repository.save_routing_trigger
            item.max_attempts = max(item.max_attempts, item.attempt + 1)
            item.lease_owner = None
            item.lease_expires_at = None
            item.last_error = None
            item.updated_at = now
            save(item)
        return self._view(kind, identifier, item)

"""Lease and execute durable Routing triggers outside database transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from javes_main.application import CaseManager
from javes_main.domain import RoutingTriggerState
from javes_main.persistence import RevisionConflict
from javes_main.ports import CaseRepository, Clock


@dataclass(frozen=True)
class RoutingDispatchReport:
    claimed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0


class RoutingTriggerDispatcher:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        manager: CaseManager,
        clock: Clock,
        worker_id: str = "in-process-routing",
        lease_s: int = 30,
    ) -> None:
        self.repository = repository
        self.manager = manager
        self.clock = clock
        self.worker_id = worker_id
        self.lease_s = lease_s

    def run_once(self, *, limit: int = 100) -> RoutingDispatchReport:
        now = self.clock.now()
        self.repository.reclaim_expired_routing_triggers(now=now)
        triggers = self.repository.claim_routing_triggers(
            worker_id=self.worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=self.lease_s),
            limit=limit,
        )
        completed = retried = failed = 0
        for trigger in triggers:
            try:
                self.manager.process_routing_trigger(trigger)
                with self.repository.transaction():
                    try:
                        current = self.repository.get_routing_trigger(trigger.trigger_id)
                    except KeyError:
                        # A concurrent successful case deletion purges all
                        # remaining Routing work; that is a completed outcome.
                        completed += 1
                        continue
                    current.state = RoutingTriggerState.DONE
                    current.lease_owner = None
                    current.lease_expires_at = None
                    current.last_error = None
                    current.updated_at = self.clock.now()
                    self.repository.save_routing_trigger(current)
                completed += 1
            except Exception as exc:
                try:
                    current = self.repository.get_routing_trigger(trigger.trigger_id)
                except KeyError:
                    completed += 1
                    continue
                current.lease_owner = None
                current.lease_expires_at = None
                current.last_error = f"{type(exc).__name__}: {exc}"
                current.updated_at = self.clock.now()
                if current.attempt >= current.max_attempts:
                    current.state = RoutingTriggerState.FAILED
                    failed += 1
                else:
                    current.state = RoutingTriggerState.PENDING
                    retried += 1
                self.repository.save_routing_trigger(current)
        return RoutingDispatchReport(
            claimed=len(triggers),
            completed=completed,
            retried=retried,
            failed=failed,
        )

    def drain(self, *, max_cycles: int = 100) -> RoutingDispatchReport:
        total = RoutingDispatchReport()
        for _ in range(max_cycles):
            report = self.run_once()
            total = RoutingDispatchReport(
                claimed=total.claimed + report.claimed,
                completed=total.completed + report.completed,
                retried=total.retried + report.retried,
                failed=total.failed + report.failed,
            )
            if report.claimed == 0 or report.retried:
                return total
        raise RuntimeError("Routing triggers did not become quiescent")

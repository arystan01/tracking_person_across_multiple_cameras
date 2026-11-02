"""One bounded scheduler iteration suitable for a process loop or tests."""

from __future__ import annotations

from dataclasses import dataclass

from javes_main.application import CaseManager

from .dispatcher import DispatchReport, OutboxDispatcher
from .routing_triggers import RoutingDispatchReport, RoutingTriggerDispatcher


@dataclass(frozen=True)
class SchedulerReport:
    perception_jobs: int
    open_updates: int
    heartbeats: int
    retention_expiries: int
    outbox: DispatchReport
    routing: RoutingDispatchReport


class SchedulerRunner:
    def __init__(
        self,
        manager: CaseManager,
        outbox: OutboxDispatcher,
        routing: RoutingTriggerDispatcher,
    ) -> None:
        self.manager = manager
        self.outbox = outbox
        self.routing = routing

    def run_case_once(self, case_id: str, *, job_limit: int = 100) -> SchedulerReport:
        first_outbox = self.outbox.drain()
        first_routing = self.routing.drain()
        jobs = self.manager.run_pending_jobs(case_id, limit=job_limit)
        second_routing = self.routing.drain()
        second_outbox = self.outbox.drain()
        return SchedulerReport(
            perception_jobs=len(jobs),
            open_updates=0,
            heartbeats=0,
            retention_expiries=0,
            outbox=DispatchReport(
                claimed=first_outbox.claimed + second_outbox.claimed,
                delivered=first_outbox.delivered + second_outbox.delivered,
                retried=first_outbox.retried + second_outbox.retried,
                failed=first_outbox.failed + second_outbox.failed,
            ),
            routing=RoutingDispatchReport(
                claimed=first_routing.claimed + second_routing.claimed,
                completed=first_routing.completed + second_routing.completed,
                retried=first_routing.retried + second_routing.retried,
                failed=first_routing.failed + second_routing.failed,
            ),
        )

    def heartbeat(self, case_id: str) -> SchedulerReport:
        self.manager.heartbeat(case_id)
        report = self.run_case_once(case_id)
        return SchedulerReport(
            perception_jobs=report.perception_jobs,
            open_updates=report.open_updates,
            heartbeats=1,
            retention_expiries=report.retention_expiries,
            outbox=report.outbox,
            routing=report.routing,
        )

    def run_once(self, *, job_limit: int = 100) -> SchedulerReport:
        now = self.manager.clock.now()
        open_updates = len(self.manager.poll_open_jobs())
        heartbeats = 0
        for case in self.manager.repository.list_cases(lifecycles={"SEARCHING"}):
            if case.next_heartbeat_at is not None and case.next_heartbeat_at <= now:
                self.manager.heartbeat(case.request.case_id)
                heartbeats += 1
        retention_expiries = self.manager.enqueue_due_retention()
        jobs = self.manager.run_pending_jobs(limit=job_limit)
        routing = self.routing.drain()
        return SchedulerReport(
            perception_jobs=len(jobs),
            open_updates=open_updates,
            heartbeats=heartbeats,
            retention_expiries=retention_expiries,
            outbox=self.outbox.drain(),
            routing=routing,
        )

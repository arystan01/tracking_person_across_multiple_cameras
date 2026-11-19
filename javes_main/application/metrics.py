"""Operational metrics derived from durable state, without a vendor backend."""

from __future__ import annotations

from collections import Counter
from typing import Any

from javes_main.domain import CaseLifecycle, JobState, OutboxState
from javes_main.ports import CaseRepository, Clock


def _duration_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "avg_s": 0.0, "max_s": 0.0}
    return {
        "count": len(values),
        "avg_s": sum(values) / len(values),
        "max_s": max(values),
    }


class MetricsService:
    def __init__(self, repository: CaseRepository, clock: Clock) -> None:
        self.repository = repository
        self.clock = clock

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        cases = self.repository.list_cases()
        case_counts = Counter(item.lifecycle.value for item in cases)
        jobs = [
            job
            for case in cases
            for job in self.repository.list_jobs(case.request.case_id)
        ]
        job_counts = Counter(item.state.value for item in jobs)
        outbox = self.repository.list_outbox()
        outbox_counts = Counter(item.state.value for item in outbox)
        routing_triggers = self.repository.list_routing_triggers()
        routing_trigger_counts = Counter(item.state.value for item in routing_triggers)
        pending_times = [
            item.created_at
            for item in jobs
            if item.state in {JobState.PENDING, JobState.LEASED}
        ]
        pending_times.extend(
            item.created_at
            for item in outbox
            if item.state in {OutboxState.PENDING, OutboxState.LEASED}
        )
        pending_times.extend(
            item.created_at
            for item in routing_triggers
            if item.state.value in {"PENDING", "LEASED"}
        )
        job_latencies = [
            max((item.updated_at - item.created_at).total_seconds(), 0.0)
            for item in jobs
            if item.state not in {JobState.PENDING, JobState.LEASED}
        ]
        outbox_latencies = [
            max((item.updated_at - item.created_at).total_seconds(), 0.0)
            for item in outbox
            if item.state in {OutboxState.DELIVERED, OutboxState.FAILED}
        ]
        routing_latencies = [
            max((item.updated_at - item.created_at).total_seconds(), 0.0)
            for item in routing_triggers
            if item.state.value in {"DONE", "FAILED"}
        ]
        processed_by_probe: dict[tuple[str, str], float] = {}
        for case in cases:
            for record in self.repository.list_probe_results(case.request.case_id):
                key = (case.request.case_id, record.result.probe_id)
                processed_by_probe[key] = max(
                    processed_by_probe.get(key, 0.0),
                    float(record.result.processed_duration_s or 0.0),
                )
        camera_seconds = sum(processed_by_probe.values())
        retries = sum(max(item.attempt - 1, 0) for item in jobs)
        outbox_retries = sum(max(item.attempt - 1, 0) for item in outbox)
        routing_retries = sum(max(item.attempt - 1, 0) for item in routing_triggers)
        unobserved = sum(
            len(case.latest_route.step_result.estimate.unobserved_windows)
            if case.latest_route is not None
            else 0
            for case in cases
        )
        duplicate_results = sum(
            1
            for case in cases
            for event in self.repository.list_events(case.request.case_id)
            if event.event_type == "WORKER_RESULT_DUPLICATE"
        )
        expired_leases = sum(
            1
            for item in [*jobs, *outbox, *routing_triggers]
            if item.lease_expires_at is not None and item.lease_expires_at <= now
        )
        deletion_failures = sum(
            1
            for item in outbox
            if item.state == OutboxState.FAILED
            and item.topic in {"RETENTION_COMMAND", "RETENTION_REGISTER"}
        )
        archive_availability = Counter()
        for case in cases:
            for event in self.repository.list_events(case.request.case_id):
                if event.event_type == "ARCHIVE_SEGMENT_RESULT":
                    availability = str(event.payload.get("availability", "UNKNOWN"))
                    archive_availability[availability] += 1
        return {
            "generated_at": now.isoformat(),
            "active_cases": sum(
                case_counts.get(state.value, 0)
                for state in (CaseLifecycle.PREPARING, CaseLifecycle.SEARCHING)
            ),
            "cases_by_lifecycle": dict(sorted(case_counts.items())),
            "jobs_by_state": dict(sorted(job_counts.items())),
            "outbox_by_state": dict(sorted(outbox_counts.items())),
            "routing_triggers_by_state": dict(sorted(routing_trigger_counts.items())),
            "oldest_queue_age_s": (
                max((now - min(pending_times)).total_seconds(), 0.0)
                if pending_times
                else 0.0
            ),
            "job_completion_latency": _duration_summary(job_latencies),
            "outbox_delivery_latency": _duration_summary(outbox_latencies),
            "routing_trigger_latency": _duration_summary(routing_latencies),
            "camera_seconds_processed": camera_seconds,
            "job_retries": retries,
            "outbox_retries": outbox_retries,
            "routing_retries": routing_retries,
            "duplicate_results": duplicate_results,
            "unobserved_windows": unobserved,
            "queue_depth": {
                "jobs": job_counts.get(JobState.PENDING.value, 0),
                "outbox": outbox_counts.get(OutboxState.PENDING.value, 0),
                "routing": routing_trigger_counts.get("PENDING", 0),
            },
            "leased_tasks": {
                "jobs": job_counts.get(JobState.LEASED.value, 0),
                "outbox": outbox_counts.get(OutboxState.LEASED.value, 0),
                "routing": routing_trigger_counts.get("LEASED", 0),
            },
            "expired_leases": expired_leases,
            "terminal_failures": {
                "jobs": job_counts.get(JobState.FAILED.value, 0)
                + job_counts.get(JobState.UNAVAILABLE.value, 0),
                "outbox": outbox_counts.get(OutboxState.FAILED.value, 0),
                "routing": routing_trigger_counts.get("FAILED", 0),
            },
            "deletion_failures": deletion_failures,
            "archive_availability": dict(sorted(archive_availability.items())),
            "routing_runs": sum(
                len(
                    self.repository.list_case_data(
                        case.request.case_id, kinds={"ROUTING_RUN"}
                    )
                )
                for case in cases
            ),
        }

from __future__ import annotations

from datetime import timedelta

from javes_contracts import CaseCommandBody, PerceptionProbeJob, ProbeRequest, RetentionResource
from javes_main.domain import JobRecord, JobState, OutboxRecord, OutboxState, OutboxTopic

from tests.test_main_infra import BASE, create_body, system


def test_scheduler_runs_due_heartbeat_and_global_job_queue():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="scheduler-create")
    # First durable dispatch makes the gallery ready and emits the initial probe.
    # Gallery creation and reference-image retention registration are both
    # durable outbox messages.
    assert app.outbox_dispatcher.run_once().delivered == 2
    assert app.routing_dispatcher.run_once().completed == 1
    case = app.repository.get("case-1")
    assert case.next_heartbeat_at is not None
    app.clock.set(case.next_heartbeat_at)

    report = app.scheduler.run_once()
    assert report.heartbeats == 1
    assert report.perception_jobs >= 1
    assert app.repository.list_probe_results("case-1")


def test_expired_job_deadline_becomes_timeout_without_calling_worker():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="deadline-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    job = app.repository.list_jobs("case-1", states={JobState.PENDING.value})[0]
    job.payload = job.payload.model_copy(
        update={"deadline": app.clock.now() - timedelta(seconds=1)}
    )
    app.repository.save_job(job)

    app.manager.run_pending_jobs("case-1", limit=1)
    expired = app.repository.get_job(job.job_id)
    assert expired.state == JobState.TIMEOUT
    accepted = app.repository.list_probe_results("case-1")
    assert accepted[-1].result.error_code == "JOB_DEADLINE_EXPIRED"


def test_outbox_failure_is_retried_and_then_acknowledged():
    app = system()
    now = app.clock.now()
    resource = RetentionResource(
        resource_id="retry-resource",
        case_id="case-retry",
        kind="TEST",
        retention_class="EPHEMERAL",
    )
    message = OutboxRecord(
        message_id="retry-message",
        case_id="case-retry",
        topic=OutboxTopic.RETENTION_REGISTER.value,
        dedupe_key="retry-message",
        payload={"resource": resource.model_dump(mode="json")},
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    app.repository.add_outbox(message)

    original = app.outbox_dispatcher.retention

    class FailOnce:
        def __init__(self):
            self.failed = False

        def register(self, value):
            if not self.failed:
                self.failed = True
                raise RuntimeError("temporary retention outage")
            return original.register(value)

        def process(self, command):
            return original.process(command)

    app.outbox_dispatcher.retention = FailOnce()
    first = app.outbox_dispatcher.run_once()
    assert first.retried == 1
    assert app.repository.get_outbox("retry-message").state == OutboxState.PENDING

    app.clock.set(now + timedelta(seconds=6))
    second = app.outbox_dispatcher.run_once()
    assert second.delivered == 1
    assert app.repository.get_outbox("retry-message").state == OutboxState.DELIVERED


def test_misrouted_worker_result_is_not_applied_to_case():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="misrouted-create")
    original = app.outbox_dispatcher.perception

    class WrongCaseGallery:
        def update_gallery(self, command):
            return original.update_gallery(command).model_copy(
                update={"case_id": "another-case"}
            )

        def execute(self, job, now):
            return original.execute(job, now)

        def cancel(self, job, now):
            return original.cancel(job, now)

    app.outbox_dispatcher.perception = WrongCaseGallery()
    report = app.outbox_dispatcher.run_once()
    assert report.retried == 1
    assert app.manager.get_view("case-1").lifecycle.value == "PREPARING"
    message = next(
        item
        for item in app.repository.list_outbox(case_id="case-1")
        if item.topic == OutboxTopic.TARGET_GALLERY.value
    )
    assert message.state == OutboxState.PENDING
    assert "does not match" in (message.last_error or "")


def test_crash_after_worker_success_before_ack_replays_idempotently():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="crash-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    app.manager.run_pending_jobs("case-1")
    app.routing_dispatcher.drain()

    original_apply = app.manager.apply_outbox_result
    failed = {"value": False}

    def fail_once(message, result):
        if message.topic == OutboxTopic.CLIP_CUT.value and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated process crash after Clip success")
        return original_apply(message, result)

    app.manager.apply_outbox_result = fail_once
    first = app.outbox_dispatcher.run_once()
    assert first.retried == 1
    assert len(app.clip_worker.records) == 1
    assert app.manager.get_view("case-1").clips == []

    app.clock.set(app.clock.now() + timedelta(seconds=6))
    second = app.outbox_dispatcher.run_once()
    assert second.delivered == 1
    assert len(app.clip_worker.records) == 1
    assert len(app.manager.get_view("case-1").clips) == 1


def test_routing_trigger_crash_after_cas_does_not_advance_twice():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="routing-crash-create")
    app.outbox_dispatcher.drain()
    now = app.clock.now()
    trigger = app.repository.claim_routing_triggers(
        worker_id="crashing-routing-worker",
        now=now,
        lease_expires_at=now + timedelta(seconds=5),
        limit=1,
    )[0]
    app.manager.process_routing_trigger(trigger)
    revision_after_cas = app.manager.get_view("case-1").route_revision
    assert revision_after_cas == 1

    app.clock.set(now + timedelta(seconds=6))
    replay = app.routing_dispatcher.run_once()
    assert replay.completed == 1
    assert app.manager.get_view("case-1").route_revision == revision_after_cas
    runs = app.repository.list_case_data("case-1", kinds={"ROUTING_RUN"})
    assert [item.record_id for item in runs] == [trigger.trigger_id]


def test_close_cancels_open_subscription_through_outbox():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="open-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    case = app.repository.get("case-1")
    open_job = JobRecord(
        job_id="open-job",
        case_id="case-1",
        dedupe_key="perception:case-1:open-probe",
        payload=PerceptionProbeJob(
            job_id="open-job",
            case_id="case-1",
            target_gallery_ref=case.gallery_ref,
            probe=ProbeRequest(
                probe_id="open-probe",
                camera_id="B",
                t_from=app.clock.now() - timedelta(seconds=10),
                t_to="OPEN",
            ),
        ),
        created_at=app.clock.now(),
        updated_at=app.clock.now(),
    )
    app.repository.add_job(open_job)
    app.manager.run_pending_jobs("case-1")
    assert app.repository.get_job("open-job").state == JobState.LEASED

    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="close-open",
            action="CLOSE",
            operator_id="operator-1",
            occurred_at=app.clock.now(),
        ),
        idempotency_key="close-open",
    )
    assert app.repository.get_job("open-job").state == JobState.CANCELLED
    app.outbox_dispatcher.drain()
    assert any(
        event["event_type"] == "PERCEPTION_SUBSCRIPTION_CANCELLED"
        for event in app.manager.get_events("case-1")
    )


def test_delete_waits_for_prior_artifact_registration_and_leaves_no_active_outbox():
    app = system()
    app.manager.create_case(create_body(), idempotency_key="delete-barrier-create")
    app.outbox_dispatcher.drain()
    app.routing_dispatcher.drain()
    app.manager.run_pending_jobs("case-1")
    app.routing_dispatcher.drain()
    # CLIP_CUT and route registrations are intentionally still pending.
    assert any(
        item.state == OutboxState.PENDING
        for item in app.repository.list_outbox(case_id="case-1")
    )
    app.manager.command(
        "case-1",
        CaseCommandBody(
            command_id="delete-with-pending-artifacts",
            action="DELETE",
            operator_id="operator-1",
            occurred_at=app.clock.now(),
        ),
        idempotency_key="delete-with-pending-artifacts",
    )
    app.outbox_dispatcher.drain()

    assert app.manager.get_view("case-1").lifecycle.value == "DELETED"
    assert all(
        item.state not in {OutboxState.PENDING, OutboxState.LEASED}
        for item in app.repository.list_outbox(case_id="case-1")
    )
    resources = [
        item
        for item in app.retention_worker.resources.values()
        if item.case_id == "case-1" and not item.legal_hold
    ]
    assert resources
    assert all(item.deleted for item in resources)

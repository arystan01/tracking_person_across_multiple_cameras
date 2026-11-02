from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from javes_contracts import (
    ArtifactRef,
    CaseWindow,
    CreateCaseBody,
    LastKnown,
    PerceptionProbeJob,
    ProbeRequest,
)
from javes_main.adapters.artifacts import FileArtifactStore
from javes_main.domain import (
    AuditEvent,
    CaseDataKind,
    CaseDataRecord,
    CaseLifecycle,
    CaseRecord,
    JobRecord,
    JobState,
    OutboxRecord,
    OutboxState,
)
from javes_main.persistence import InMemoryRepository, RevisionConflict
from javes_main.persistence.migrate import upgrade_database
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository


UTC = timezone.utc
NOW = datetime(2026, 10, 16, 5, 0, tzinfo=UTC)


def repository() -> SqlAlchemyRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repo = SqlAlchemyRepository(engine)
    repo.create_schema()
    return repo


def case_record() -> CaseRecord:
    request = CreateCaseBody(
        case_id="db-case",
        operator_id="operator-1",
        target_id="target-1",
        reference_image_ref=ArtifactRef(object_key="cases/db-case/reference.jpg"),
        last_known=LastKnown(camera_id="A", time=NOW),
        window=CaseWindow(start=NOW, end=NOW + timedelta(minutes=10)),
        site_id="site-1",
        site_version="v1",
    )
    return CaseRecord(
        request=request,
        lifecycle=CaseLifecycle.SEARCHING,
        created_at=NOW,
        updated_at=NOW,
    )


def test_sqlalchemy_repository_roundtrip_cas_jobs_events_and_idempotency():
    repo = repository()
    case = case_record()
    repo.add(case)
    assert repo.get("db-case") == case

    saved = repo.save(case.model_copy(update={"lifecycle": CaseLifecycle.CLOSED}), 0)
    assert saved.revision == 1
    assert repo.get("db-case").lifecycle == CaseLifecycle.CLOSED
    with pytest.raises(RevisionConflict):
        repo.save(case, 0)

    probe = ProbeRequest(
        probe_id="probe-1",
        camera_id="B",
        t_from=NOW,
        t_to=NOW + timedelta(seconds=30),
    )
    job = JobRecord(
        job_id="job-1",
        case_id="db-case",
        dedupe_key="perception:db-case:probe-1",
        payload=PerceptionProbeJob(
            job_id="job-1",
            case_id="db-case",
            target_gallery_ref="gallery-1",
            probe=probe,
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    repo.add_job(job)
    repo.add_job(job)
    assert repo.get_job("job-1") == job
    assert repo.list_jobs("db-case", states={"PENDING"}) == [job]

    event = AuditEvent(
        event_id="event-1",
        case_id="db-case",
        event_type="TEST",
        occurred_at=NOW,
    )
    repo.append_event(event)
    repo.append_event(event)
    assert repo.list_events("db-case") == [event]

    repo.remember_idempotency("key-1", "fingerprint-1", {"ok": True})
    repo.remember_idempotency("key-1", "fingerprint-1", {"ignored": True})
    assert repo.get_idempotency("key-1") == ("fingerprint-1", {"ok": True})
    with pytest.raises(ValueError, match="idempotency"):
        repo.remember_idempotency("key-1", "different", {})


@pytest.mark.parametrize("repo_factory", [InMemoryRepository, repository])
def test_job_leases_are_exclusive_and_expire_with_attempt_limit(repo_factory):
    repo = repo_factory()
    job = JobRecord(
        job_id="lease-job",
        case_id="lease-case",
        dedupe_key="perception:lease-case:probe-1",
        max_attempts=2,
        payload=PerceptionProbeJob(
            job_id="lease-job",
            case_id="lease-case",
            target_gallery_ref="gallery-1",
            probe=ProbeRequest(
                probe_id="probe-1",
                camera_id="B",
                t_from=NOW,
                t_to=NOW + timedelta(seconds=30),
            ),
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    repo.add_job(job)

    first = repo.claim_jobs(
        worker_id="worker-1",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=10),
        limit=1,
    )
    assert len(first) == 1
    assert first[0].state == JobState.LEASED
    assert first[0].attempt == 1
    assert repo.claim_jobs(
        worker_id="worker-2",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=10),
        limit=1,
    ) == []

    reclaimed = repo.reclaim_expired_jobs(now=NOW + timedelta(seconds=11))
    assert reclaimed[0].state == JobState.PENDING
    second = repo.claim_jobs(
        worker_id="worker-2",
        now=NOW + timedelta(seconds=11),
        lease_expires_at=NOW + timedelta(seconds=20),
        limit=1,
    )
    assert second[0].attempt == 2
    exhausted = repo.reclaim_expired_jobs(now=NOW + timedelta(seconds=21))
    assert exhausted[0].state == JobState.UNAVAILABLE
    assert repo.claim_jobs(
        worker_id="worker-3",
        now=NOW + timedelta(seconds=21),
        lease_expires_at=NOW + timedelta(seconds=30),
        limit=1,
    ) == []


def test_file_artifact_store_is_atomic_idempotent_and_blocks_traversal(tmp_path):
    store = FileArtifactStore(tmp_path / "artifacts")
    key = "cases/case-1/routes/1/1F.svg"
    assert store.put(key, b"<svg/>", "image/svg+xml") == key
    assert store.put(key, b"<svg/>", "image/svg+xml") == key
    assert store.get(key) == (b"<svg/>", "image/svg+xml")
    with pytest.raises(ValueError, match="different content"):
        store.put(key, b"different", "image/svg+xml")
    with pytest.raises(ValueError, match="escapes"):
        store.put("../outside", b"bad", "application/octet-stream")
    assert store.delete(key) is True
    assert store.delete(key) is False
    with pytest.raises(KeyError, match="unknown artifact"):
        store.get(key)
    with pytest.raises(ValueError, match="escapes"):
        store.delete("../outside")


@pytest.mark.parametrize("repo_factory", [InMemoryRepository, repository])
def test_transaction_rolls_back_case_and_event(repo_factory):
    repo = repo_factory()
    with pytest.raises(RuntimeError, match="rollback"):
        with repo.transaction():
            repo.add(case_record())
            repo.append_event(
                AuditEvent(
                    event_id="rollback-event",
                    case_id="db-case",
                    event_type="ROLLBACK",
                    occurred_at=NOW,
                )
            )
            raise RuntimeError("rollback")
    with pytest.raises(KeyError):
        repo.get("db-case")
    assert repo.list_events("db-case") == []


@pytest.mark.parametrize("repo_factory", [InMemoryRepository, repository])
def test_case_purge_keeps_complete_append_only_audit(repo_factory):
    repo = repo_factory()
    repo.add(case_record())
    event = AuditEvent(
        event_id="permanent-audit-event",
        case_id="db-case",
        event_type="CASE_DELETE_REQUESTED",
        actor_id="root-1",
        occurred_at=NOW,
        payload={"resource_id": "route-7", "reason": "operator-request"},
    )
    repo.append_event(event)

    repo.purge_case_content("db-case")

    assert repo.list_events("db-case") == [event]


@pytest.mark.parametrize("repo_factory", [InMemoryRepository, repository])
def test_case_data_ids_are_scoped_to_case(repo_factory):
    repo = repo_factory()
    for case_id in ("case-a", "case-b"):
        repo.add_case_data(
            CaseDataRecord(
                record_id="same-worker-id",
                case_id=case_id,
                kind=CaseDataKind.TRACKLET,
                occurred_at=NOW,
                payload={"case": case_id},
            )
        )
    assert repo.list_case_data("case-a")[0].payload == {"case": "case-a"}
    assert repo.list_case_data("case-b")[0].payload == {"case": "case-b"}


@pytest.mark.parametrize("repo_factory", [InMemoryRepository, repository])
def test_outbox_is_deduplicated_leased_and_recovered(repo_factory):
    repo = repo_factory()
    message = OutboxRecord(
        message_id="message-1",
        case_id="case-1",
        topic="RETENTION_COMMAND",
        dedupe_key="retention:case-1:close-1",
        payload={"command_id": "close-1"},
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        max_attempts=2,
    )
    repo.add_outbox(message)
    repo.add_outbox(message)
    claimed = repo.claim_outbox(
        worker_id="dispatcher-1",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=10),
        limit=10,
    )
    assert len(claimed) == 1
    assert claimed[0].state == OutboxState.LEASED
    assert repo.claim_outbox(
        worker_id="dispatcher-2",
        now=NOW,
        lease_expires_at=NOW + timedelta(seconds=10),
        limit=10,
    ) == []
    assert repo.reclaim_expired_outbox(now=NOW + timedelta(seconds=11))[0].state == OutboxState.PENDING
    repo.claim_outbox(
        worker_id="dispatcher-2",
        now=NOW + timedelta(seconds=11),
        lease_expires_at=NOW + timedelta(seconds=20),
        limit=10,
    )
    assert repo.reclaim_expired_outbox(now=NOW + timedelta(seconds=21))[0].state == OutboxState.FAILED


def test_packaged_migration_entrypoint(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'main.sqlite'}"
    upgrade_database(database_url)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == (
            "0006_routing_triggers"
        )
        tables = {
            row[0]
            for row in connection.execute(
                text("select name from sqlite_master where type='table'")
            )
        }
    assert {
        "cases",
        "jobs",
        "case_events",
        "idempotency_keys",
        "users",
        "auth_sessions",
        "outbox",
        "probe_results",
        "case_data",
        "routing_triggers",
    } <= tables

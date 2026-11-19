from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from javes_contracts.trace import bind_request_context, reset_request_context
from javes_main.adapters.system import FixedClock
from javes_main.application import DeadLetterService
from javes_main.domain import OutboxRecord, OutboxState
from javes_main.observability import JsonFormatter, redact
from javes_main.persistence.backup import backup_database, restore_database
from javes_main.persistence.memory import InMemoryRepository
from javes_main.security import scan_files
from javes_workers import HttpWorkerClient


NOW = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)


def test_redaction_and_structured_logs_never_expose_boundary_secrets() -> None:
    secret = "top-secret-worker-token"
    assert redact({"authorization": f"Bearer {secret}", "nested": {"password": secret}}) == {
        "authorization": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
    record = logging.LogRecord(
        "javes", logging.ERROR, __file__, 1, f"request Bearer {secret} failed", (), None
    )
    output = JsonFormatter().format(record)
    assert secret not in output
    assert json.loads(output)["message"] == "request Bearer [REDACTED] failed"


def test_trace_context_propagates_to_worker_http_client() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"contract_version": "worker-claim-result.v1", "tasks": []})

    tokens = bind_request_context(
        "case-correlation-001",
        "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    )
    try:
        client = HttpWorkerClient(
            "https://main.invalid",
            "retention",
            "example-token",
            "retention-01",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert client.claim() == []
    finally:
        reset_request_context(tokens)
    assert seen["x-request-id"] == "case-correlation-001"
    assert seen["traceparent"].endswith("-01")


def test_dead_letter_inspect_and_explicit_replay() -> None:
    repository = InMemoryRepository()
    repository.add_outbox(
        OutboxRecord(
            message_id="message-001",
            case_id="case-001",
            topic="RETENTION_COMMAND",
            dedupe_key="retention:case-001",
            payload={"safe": True},
            state=OutboxState.FAILED,
            attempt=5,
            max_attempts=5,
            available_at=NOW,
            created_at=NOW,
            updated_at=NOW,
            last_error="UPSTREAM_TIMEOUT",
        )
    )
    service = DeadLetterService(repository, FixedClock(NOW + timedelta(minutes=1)))
    assert service.list()[0]["last_error"] == "UPSTREAM_TIMEOUT"
    replayed = service.replay("outbox", "message-001")
    assert replayed["last_error"] is None
    record = repository.get_outbox("message-001")
    assert record.state == OutboxState.PENDING
    assert record.max_attempts == 6
    with pytest.raises(ValueError, match="only failed"):
        service.replay("outbox", "message-001")


def test_bounded_multi_worker_lease_soak_has_no_duplicate_claims() -> None:
    repository = InMemoryRepository()
    for index in range(100):
        repository.add_outbox(
            OutboxRecord(
                message_id=f"soak-{index:03d}",
                case_id="case-soak",
                topic="RETENTION_COMMAND",
                dedupe_key=f"soak-dedupe-{index:03d}",
                payload={"index": index},
                available_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    barrier = Barrier(4)

    def claim(worker_id: str):
        barrier.wait()
        return repository.claim_outbox(
            worker_id=worker_id,
            now=NOW,
            lease_expires_at=NOW + timedelta(seconds=5),
            limit=100,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(claim, ["worker-1", "worker-2", "worker-3", "worker-4"]))
    identifiers = [item.message_id for batch in batches for item in batch]
    assert len(identifiers) == 100
    assert len(set(identifiers)) == 100
    assert len(repository.reclaim_expired_outbox(now=NOW + timedelta(seconds=6))) == 100
    recovered = repository.claim_outbox(
        worker_id="recovery",
        now=NOW + timedelta(seconds=6),
        lease_expires_at=NOW + timedelta(seconds=12),
        limit=100,
    )
    assert {item.message_id for item in recovered} == set(identifiers)


def test_sqlite_backup_is_verified_and_really_restorable(tmp_path: Path) -> None:
    live = tmp_path / "live.db"
    with sqlite3.connect(live) as connection:
        connection.execute("create table evidence (id text primary key, value text)")
        connection.execute("insert into evidence values ('one', 'preserved')")
    backup = backup_database(f"sqlite:///{live}", tmp_path / "backup.db")
    with sqlite3.connect(live) as connection:
        connection.execute("delete from evidence")
    restore_database(f"sqlite:///{live}", backup)
    with sqlite3.connect(live) as connection:
        assert connection.execute("select * from evidence").fetchall() == [
            ("one", "preserved")
        ]
    backup.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_database(f"sqlite:///{live}", backup)


def test_secret_scanner_reports_locations_without_echoing_secret(tmp_path: Path) -> None:
    source = tmp_path / "config.py"
    fake_secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    source.write_text(f"TOKEN = '{fake_secret}'\n")
    findings = scan_files([source])
    assert findings == [f"{source}:1: github-token"]
    assert "ghp_" not in findings[0]

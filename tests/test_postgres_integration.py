from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from javes_main.domain import OutboxRecord
from javes_main.persistence.migrate import downgrade_database, upgrade_database
from javes_main.persistence.backup import backup_database, restore_database
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository


DATABASE_URL = os.environ.get("JAVES_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="JAVES_TEST_POSTGRES_URL is not set")


def test_postgres_migration_contention_lease_recovery_and_restore(tmp_path) -> None:
    assert DATABASE_URL is not None
    downgrade_database(DATABASE_URL, "base")
    upgrade_database(DATABASE_URL, "head")
    first = SqlAlchemyRepository.from_url(DATABASE_URL, pool_pre_ping=True)
    second = SqlAlchemyRepository.from_url(DATABASE_URL, pool_pre_ping=True)
    now = datetime.now(timezone.utc)
    for index in range(20):
        first.add_outbox(
            OutboxRecord(
                message_id=f"message-{index:02d}",
                case_id="case-contention",
                topic="RETENTION_COMMAND",
                dedupe_key=f"dedupe-{index:02d}",
                payload={"index": index},
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    def claim(repository, worker):
        return repository.claim_outbox(
            worker_id=worker,
            now=now,
            lease_expires_at=now + timedelta(seconds=5),
            limit=20,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(lambda args: claim(*args), [(first, "one"), (second, "two")]))
    identifiers = [item.message_id for batch in batches for item in batch]
    assert len(identifiers) == 20
    assert len(set(identifiers)) == 20

    reclaimed = first.reclaim_expired_outbox(now=now + timedelta(seconds=6))
    assert {item.message_id for item in reclaimed} == set(identifiers)
    claimed_again = second.claim_outbox(
        worker_id="recovery",
        now=now + timedelta(seconds=6),
        lease_expires_at=now + timedelta(seconds=12),
        limit=20,
    )
    assert {item.message_id for item in claimed_again} == set(identifiers)

    backup = backup_database(DATABASE_URL, tmp_path / "postgres.dump")
    first.engine.dispose()
    second.engine.dispose()
    downgrade_database(DATABASE_URL, "base")
    restore_database(DATABASE_URL, backup)
    restored = SqlAlchemyRepository.from_url(DATABASE_URL, pool_pre_ping=True)
    assert {item.message_id for item in restored.list_outbox()} == set(identifiers)
    restored.engine.dispose()

    downgrade_database(DATABASE_URL, "base")
    upgrade_database(DATABASE_URL, "head")

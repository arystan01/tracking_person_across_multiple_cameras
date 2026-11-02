"""Durable SQLite inventory and deletion journal for Retention."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from javes_contracts import RetentionCommand, RetentionResource, RetentionResult


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _loads(value: str | None, default: Any = None) -> Any:
    return default if value is None else json.loads(value)


@dataclass(frozen=True)
class StoredResource:
    resource: RetentionResource
    state: str
    registered_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    attempt: int
    last_error: str | None
    proof: list[dict[str, Any]]


@dataclass(frozen=True)
class StoredCommand:
    command: RetentionCommand
    fingerprint: str
    state: str
    result: RetentionResult | None
    last_error: str | None


@dataclass(frozen=True)
class CommandItem:
    command_id: str
    resource_id: str
    disposition: str
    state: str
    proof: list[dict[str, Any]]
    last_error: str | None


@dataclass(frozen=True)
class CaseMarker:
    case_id: str
    closed_at: datetime | None = None
    deleted_at: datetime | None = None
    ttl_expired_at: datetime | None = None


class SQLiteRetentionRepository:
    """Owns Retention metadata; artifact bytes stay in configured backends.

    SQLite is sufficient for the local modular-monolith deployment and tests.
    All destructive work is journalled before an external delete call, so a
    process crash resumes safely.  A transport deployment can replace this
    class without changing the worker or boundary contracts.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self):
        """Cross-process write lock for a short inventory decision."""

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS retention_resources (
                resource_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                proof TEXT NOT NULL DEFAULT '[]'
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_retention_resources_case ON retention_resources(case_id)",
            """
            CREATE TABLE IF NOT EXISTS retention_cases (
                case_id TEXT PRIMARY KEY,
                closed_at TEXT,
                deleted_at TEXT,
                ttl_expired_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS retention_commands (
                command_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result TEXT,
                last_error TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_retention_commands_case ON retention_commands(case_id)",
            """
            CREATE TABLE IF NOT EXISTS retention_command_resources (
                command_id TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                disposition TEXT NOT NULL,
                state TEXT NOT NULL,
                proof TEXT NOT NULL DEFAULT '[]',
                last_error TEXT,
                PRIMARY KEY(command_id, resource_id),
                FOREIGN KEY(command_id) REFERENCES retention_commands(command_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS retention_tombstones (
                tombstone_id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL,
                action TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                deleted_count INTEGER NOT NULL,
                preserved_count INTEGER NOT NULL,
                proof_digest TEXT NOT NULL
            )
            """,
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    self._db.execute(statement)
                self._db.execute("PRAGMA user_version = 1")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def get_resource(self, resource_id: str) -> StoredResource | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM retention_resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
        return self._resource(row) if row is not None else None

    def list_resources(self, case_id: str | None = None) -> list[StoredResource]:
        query = "SELECT * FROM retention_resources"
        args: tuple[object, ...] = ()
        if case_id is not None:
            query += " WHERE case_id = ?"
            args = (case_id,)
        query += " ORDER BY resource_id"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [self._resource(row) for row in rows]

    @staticmethod
    def _resource(row: sqlite3.Row) -> StoredResource:
        resource = RetentionResource.model_validate_json(row["payload"])
        if row["state"] == "DELETED" and not resource.deleted:
            resource = resource.model_copy(update={"deleted": True})
        return StoredResource(
            resource=resource,
            state=row["state"],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            deleted_at=(
                datetime.fromisoformat(row["deleted_at"])
                if row["deleted_at"]
                else None
            ),
            attempt=int(row["attempt"]),
            last_error=row["last_error"],
            proof=_loads(row["proof"], []),
        )

    def save_resource(
        self,
        resource: RetentionResource,
        *,
        state: str,
        now: datetime,
        registered_at: datetime | None = None,
        deleted_at: datetime | None = None,
        attempt: int = 0,
        last_error: str | None = None,
        proof: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = resource.model_copy(update={"deleted": state == "DELETED"})
        with self._lock:
            self._db.execute(
                """
                INSERT INTO retention_resources (
                    resource_id, case_id, payload, state, registered_at,
                    updated_at, deleted_at, attempt, last_error, proof
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                    case_id=excluded.case_id,
                    payload=excluded.payload,
                    state=excluded.state,
                    updated_at=excluded.updated_at,
                    deleted_at=excluded.deleted_at,
                    attempt=excluded.attempt,
                    last_error=excluded.last_error,
                    proof=excluded.proof
                """,
                (
                    resource.resource_id,
                    resource.case_id,
                    payload.model_dump_json(),
                    state,
                    _utc_text(registered_at or now),
                    _utc_text(now),
                    _utc_text(deleted_at),
                    attempt,
                    last_error,
                    json.dumps(proof or [], sort_keys=True, separators=(",", ":")),
                ),
            )

    def get_case_marker(self, case_id: str) -> CaseMarker:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM retention_cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        if row is None:
            return CaseMarker(case_id=case_id)
        return CaseMarker(
            case_id=case_id,
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
            ttl_expired_at=(
                datetime.fromisoformat(row["ttl_expired_at"])
                if row["ttl_expired_at"]
                else None
            ),
        )

    def apply_case_marker(self, command: RetentionCommand) -> None:
        marker = self.get_case_marker(command.case_id)
        closed_at = marker.closed_at
        deleted_at = marker.deleted_at
        ttl_expired_at = marker.ttl_expired_at
        if command.action == "CLOSE_CASE":
            closed_at = min(filter(None, (closed_at, command.occurred_at)), default=command.occurred_at)
        elif command.action == "DELETE_CASE":
            deleted_at = min(filter(None, (deleted_at, command.occurred_at)), default=command.occurred_at)
        elif command.action == "TTL_EXPIRED":
            ttl_expired_at = min(
                filter(None, (ttl_expired_at, command.occurred_at)),
                default=command.occurred_at,
            )
        with self._lock:
            self._db.execute(
                """
                INSERT INTO retention_cases(case_id, closed_at, deleted_at, ttl_expired_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    closed_at=excluded.closed_at,
                    deleted_at=excluded.deleted_at,
                    ttl_expired_at=excluded.ttl_expired_at
                """,
                (
                    command.case_id,
                    _utc_text(closed_at),
                    _utc_text(deleted_at),
                    _utc_text(ttl_expired_at),
                ),
            )

    def get_command(self, command_id: str) -> StoredCommand | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM retention_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredCommand(
            command=RetentionCommand.model_validate_json(row["payload"]),
            fingerprint=row["fingerprint"],
            state=row["state"],
            result=(
                RetentionResult.model_validate_json(row["result"])
                if row["result"]
                else None
            ),
            last_error=row["last_error"],
        )

    def begin_command(
        self,
        command: RetentionCommand,
        fingerprint: str,
        *,
        targets: Iterable[str],
        preserved: Iterable[str],
        now: datetime,
    ) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """
                    INSERT INTO retention_commands(
                        command_id, case_id, payload, fingerprint, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'PROCESSING', ?, ?)
                    """,
                    (
                        command.command_id,
                        command.case_id,
                        command.model_dump_json(),
                        fingerprint,
                        _utc_text(now),
                        _utc_text(now),
                    ),
                )
                for resource_id in sorted(set(targets)):
                    self._db.execute(
                        """
                        INSERT INTO retention_command_resources(
                            command_id, resource_id, disposition, state
                        ) VALUES (?, ?, 'DELETE', 'PENDING')
                        """,
                        (command.command_id, resource_id),
                    )
                for resource_id in sorted(set(preserved)):
                    self._db.execute(
                        """
                        INSERT INTO retention_command_resources(
                            command_id, resource_id, disposition, state
                        ) VALUES (?, ?, 'PRESERVE', 'DONE')
                        """,
                        (command.command_id, resource_id),
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def resume_command(self, command_id: str, *, now: datetime) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE retention_commands
                SET state='PROCESSING', updated_at=?, last_error=NULL
                WHERE command_id=? AND state!='DONE'
                """,
                (_utc_text(now), command_id),
            )

    def list_command_items(self, command_id: str) -> list[CommandItem]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM retention_command_resources
                WHERE command_id = ? ORDER BY resource_id
                """,
                (command_id,),
            ).fetchall()
        return [
            CommandItem(
                command_id=row["command_id"],
                resource_id=row["resource_id"],
                disposition=row["disposition"],
                state=row["state"],
                proof=_loads(row["proof"], []),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def update_command_item(
        self,
        command_id: str,
        resource_id: str,
        *,
        state: str,
        proof: list[dict[str, Any]] | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE retention_command_resources
                SET state=?, proof=?, last_error=?
                WHERE command_id=? AND resource_id=?
                """,
                (
                    state,
                    json.dumps(proof or [], sort_keys=True, separators=(",", ":")),
                    last_error,
                    command_id,
                    resource_id,
                ),
            )

    def fail_command(self, command_id: str, *, now: datetime, error: str) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE retention_commands
                SET state='FAILED', updated_at=?, last_error=? WHERE command_id=?
                """,
                (_utc_text(now), error, command_id),
            )

    def complete_command(
        self,
        result: RetentionResult,
        *,
        now: datetime,
        policy_version: str,
        proof_digest: str,
    ) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                command_payload = self._db.execute(
                    "SELECT payload FROM retention_commands WHERE command_id=?",
                    (result.command_id,),
                ).fetchone()["payload"]
                command = RetentionCommand.model_validate_json(command_payload)
                self._db.execute(
                    """
                    INSERT INTO retention_tombstones(
                        tombstone_id, command_id, case_id, action, occurred_at,
                        completed_at, policy_version, deleted_count,
                        preserved_count, proof_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(command_id) DO NOTHING
                    """,
                    (
                        result.audit_tombstone_id,
                        result.command_id,
                        result.case_id,
                        result.action,
                        _utc_text(command.occurred_at),
                        _utc_text(now),
                        policy_version,
                        len(result.deleted_resource_ids),
                        len(result.preserved_resource_ids),
                        proof_digest,
                    ),
                )
                self._db.execute(
                    """
                    UPDATE retention_commands
                    SET state='DONE', updated_at=?, result=?, payload=?, last_error=NULL
                    WHERE command_id=?
                    """,
                    (
                        _utc_text(now),
                        result.model_dump_json(),
                        command.model_dump_json(),
                        result.command_id,
                    ),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def get_tombstone(self, tombstone_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM retention_tombstones WHERE tombstone_id = ?",
                (tombstone_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_tombstones(self, case_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM retention_tombstones"
        args: tuple[object, ...] = ()
        if case_id is not None:
            query += " WHERE case_id = ?"
            args = (case_id,)
        query += " ORDER BY completed_at, tombstone_id"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [dict(row) for row in rows]

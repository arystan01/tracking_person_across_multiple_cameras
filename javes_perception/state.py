"""Small durable state store for Perception command replay and OPEN progress."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from javes_contracts import TargetGalleryResult, WorkerTaskResultRequest


class PerceptionStateStore:
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS gallery_commands (
                command_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS open_checkpoints (
                job_id TEXT PRIMARY KEY,
                processed_until TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worker_results (
                task_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS open_results (
                job_id TEXT PRIMARY KEY,
                result_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def gallery_result(
        self, command_id: str
    ) -> tuple[str, TargetGalleryResult] | None:
        row = self.connection.execute(
            "SELECT fingerprint, result_json FROM gallery_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), TargetGalleryResult.model_validate_json(row[1])

    def save_gallery_result(
        self, command_id: str, fingerprint: str, result: TargetGalleryResult
    ) -> None:
        self.connection.execute(
            "INSERT INTO gallery_commands(command_id, fingerprint, result_json) "
            "VALUES (?, ?, ?) ON CONFLICT(command_id) DO NOTHING",
            (command_id, fingerprint, result.model_dump_json()),
        )
        self.connection.commit()

    def checkpoint(self, job_id: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT processed_until FROM open_checkpoints WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        value = datetime.fromisoformat(str(row[0]))
        return value.astimezone(timezone.utc)

    def save_checkpoint(self, job_id: str, processed_until: datetime) -> None:
        value = processed_until.astimezone(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO open_checkpoints(job_id, processed_until) VALUES (?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET processed_until = excluded.processed_until",
            (job_id, value),
        )
        self.connection.commit()

    def task_result(
        self, task_id: str
    ) -> tuple[str, WorkerTaskResultRequest] | None:
        row = self.connection.execute(
            "SELECT fingerprint, result_json FROM worker_results WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), WorkerTaskResultRequest.model_validate_json(row[1])

    def save_task_result(
        self, task_id: str, fingerprint: str, result: WorkerTaskResultRequest
    ) -> None:
        self.connection.execute(
            "INSERT INTO worker_results(task_id, fingerprint, result_json) VALUES (?, ?, ?) "
            "ON CONFLICT(task_id) DO NOTHING",
            (task_id, fingerprint, result.model_dump_json()),
        )
        self.connection.commit()

    def delete_task_result(self, task_id: str) -> None:
        self.connection.execute(
            "DELETE FROM worker_results WHERE task_id = ?", (task_id,)
        )
        self.connection.commit()

    def open_result(self, job_id: str):
        from javes_contracts import ProbeResult

        row = self.connection.execute(
            "SELECT result_json FROM open_results WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return ProbeResult.model_validate_json(row[0])

    def save_open_result(self, job_id: str, result) -> None:
        self.connection.execute(
            "INSERT INTO open_results(job_id, result_json) VALUES (?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET result_json = excluded.result_json",
            (job_id, result.model_dump_json(by_alias=True)),
        )
        self.connection.commit()

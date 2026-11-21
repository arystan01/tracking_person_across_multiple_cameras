"""Clip-owned durable metadata and shared-filesystem artifacts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from datetime import datetime, timezone


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def case_prefix(case_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        raise ValueError("case_id must contain only letters, digits, underscores or hyphens")
    return f"cases/{case_id}"


class ClipStore:
    """One SQLite database per worker deployment, outside the served artifact root.

    Serial transactions also protect concurrent instances sharing this database.
    Receipts contain identifiers/results, never footage or authentication tokens.
    """

    def __init__(self, database: str | Path, artifact_root: str | Path):
        self.root = Path(artifact_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        database = Path(database).resolve()
        if database.is_relative_to(self.root):
            raise ValueError("state database must be outside the artifact root")
        database.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit keeps media work outside write transactions; the explicit
        # transaction() context is used only for short reservation/receipt CAS.
        self.db = sqlite3.connect(database, timeout=600, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        if self.db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
            self.db.execute("INSERT INTO schema_meta VALUES (1)")
        self._add_column("clips", "case_id", "TEXT")
        self._add_column("clips", "created_at", "TEXT")
        self._add_column("clips", "updated_at", "TEXT")
        self._add_column("receipts", "case_id", "TEXT")
        self._add_column("receipts", "created_at", "TEXT")
        self._add_column("receipts", "updated_at", "TEXT")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reservations "
            "(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS purge_tombstones "
            "(case_hash TEXT PRIMARY KEY, purged_at TEXT NOT NULL)"
        )
        self.db.execute("UPDATE schema_meta SET version=2")
        self.db.commit()

    def _add_column(self, table: str, name: str, declaration: str) -> None:
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def cached(self, key: str, digest: str):
        row = self.db.execute("SELECT fingerprint,result FROM receipts WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ValueError("same request ID used with different payload")
        return json.loads(row[1])

    def reserve(self, key: str, case_id: str, digest: str) -> None:
        row = self.db.execute(
            "SELECT fingerprint FROM reservations WHERE id=?", (key,)
        ).fetchone()
        if row is not None and row[0] != digest:
            raise ValueError("same request ID used with different payload")
        if row is None:
            self.db.execute(
                "INSERT INTO reservations VALUES (?,?,?,?)",
                (key, case_id, digest, datetime.now(timezone.utc).isoformat()),
            )

    def remember(self, key: str, digest: str, result: dict, *, case_id: str | None = None):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO receipts (id,fingerprint,result,case_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (key, digest, json.dumps(result), case_id, now, now),
        )
        self.db.execute("DELETE FROM reservations WHERE id=?", (key,))

    def get_clip(self, clip_id: str):
        row = self.db.execute("SELECT record FROM clips WHERE id=?", (clip_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_clip(self, record):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO clips (id,record,case_id,created_at,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET record=excluded.record,case_id=excluded.case_id,updated_at=excluded.updated_at",
            (record.clip_id, record.model_dump_json(), record.case_id, now, now),
        )

    def purge_case(self, case_id: str) -> None:
        self.db.execute("DELETE FROM clips WHERE case_id=?", (case_id,))
        self.db.execute("DELETE FROM receipts WHERE case_id=?", (case_id,))
        self.db.execute("DELETE FROM reservations WHERE case_id=?", (case_id,))
        case_hash = hashlib.sha256(case_id.encode()).hexdigest()
        self.db.execute(
            "INSERT OR REPLACE INTO purge_tombstones VALUES (?,?)",
            (case_hash, datetime.now(timezone.utc).isoformat()),
        )

    def path(self, key: str) -> Path:
        if not key or "\\" in key or any(x in {"", ".", ".."} for x in key.split("/")):
            raise ValueError("unsafe artifact key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("artifact path escapes root")
        return path

    def publish(self, source: Path, key: str) -> Path:
        """Publish complete files and the MIME sidecar expected by Main/Retention."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        mime = self.root / '.mime' / hashlib.sha256(key.encode()).hexdigest()
        mime.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=mime.parent, prefix='.clip-')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write('video/mp4')
                f.flush()
                os.fsync(f.fileno())
            # Media temporary files live on the artifact filesystem.
            try:
                os.link(source, target)
            except FileExistsError:
                raise ValueError("artifact already exists")
            os.replace(temporary, mime)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

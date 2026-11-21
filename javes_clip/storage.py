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
        self.db = sqlite3.connect(database, timeout=600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)")
        self.db.commit()

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

    def remember(self, key: str, digest: str, result: dict):
        self.db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, digest, json.dumps(result)))

    def get_clip(self, clip_id: str):
        row = self.db.execute("SELECT record FROM clips WHERE id=?", (clip_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_clip(self, record):
        self.db.execute("INSERT OR REPLACE INTO clips VALUES (?,?)", (record.clip_id, record.model_dump_json()))

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
            os.replace(temporary, mime)
            # Media temporary files live on the artifact filesystem.
            try:
                os.link(source, target)
            except FileExistsError:
                raise ValueError("artifact already exists")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

"""Verified database backup/restore commands for operational rehearsals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy.engine import make_url


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _postgres_args(database_url: str) -> tuple[list[str], dict[str, str]]:
    url = make_url(database_url)
    args: list[str] = []
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    if url.database:
        args += ["--dbname", url.database]
    environment = dict(os.environ)
    if url.password:
        environment["PGPASSWORD"] = url.password
    return args, environment


def backup_database(database_url: str, destination: str | Path) -> Path:
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        source = Path(url.database or "").resolve(strict=True)
        if source == target:
            raise ValueError("backup destination must differ from the live database")
        with sqlite3.connect(source) as live, sqlite3.connect(target) as backup:
            live.backup(backup)
    elif url.get_backend_name() == "postgresql":
        args, environment = _postgres_args(database_url)
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--file", str(target), *args],
            check=True,
            env=environment,
        )
    else:
        raise ValueError(f"unsupported backup backend {url.get_backend_name()!r}")
    manifest = {
        "format": "javes-database-backup.v1",
        "backend": url.get_backend_name(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
    }
    target.with_suffix(target.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def restore_database(database_url: str, source: str | Path) -> None:
    backup = Path(source).resolve(strict=True)
    manifest_path = backup.with_suffix(backup.suffix + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "javes-database-backup.v1":
        raise ValueError("unknown database backup manifest")
    if manifest.get("sha256") != _sha256(backup):
        raise ValueError("database backup checksum mismatch")
    url = make_url(database_url)
    if manifest.get("backend") != url.get_backend_name():
        raise ValueError("backup backend does not match restore target")
    if url.get_backend_name() == "sqlite":
        target = Path(url.database or "").resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.restore-{os.getpid()}")
        shutil.copyfile(backup, temporary)
        with sqlite3.connect(temporary) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                temporary.unlink(missing_ok=True)
                raise ValueError("restored SQLite backup failed integrity_check")
        os.replace(temporary, target)
    elif url.get_backend_name() == "postgresql":
        args, environment = _postgres_args(database_url)
        subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", *args, str(backup)],
            check=True,
            env=environment,
        )
    else:
        raise ValueError(f"unsupported restore backend {url.get_backend_name()!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup or restore the JAVES database")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("path")
    parser.add_argument("--database-url", default=os.environ.get("JAVES_DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    if args.action == "backup":
        backup_database(args.database_url, args.path)
    else:
        restore_database(args.database_url, args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

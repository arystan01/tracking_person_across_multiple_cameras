"""Verified database backup/restore commands for operational rehearsals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, inspect, text
from javes_telemetry import configure_telemetry, operation, shutdown_telemetry


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


def _target_nonempty(database_url: str) -> bool:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        target = Path(url.database or "")
        if not target.exists() or target.stat().st_size == 0:
            return False
    engine = create_engine(database_url)
    try:
        return bool(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def verify_backup(database_url: str, source: str | Path) -> dict:
    backup = Path(source).resolve(strict=True)
    manifest_path = backup.with_suffix(backup.suffix + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "javes-database-backup.v1":
        raise ValueError("unknown database backup manifest")
    if manifest.get("sha256") != _sha256(backup):
        raise ValueError("database backup checksum mismatch")
    if manifest.get("size_bytes") != backup.stat().st_size:
        raise ValueError("database backup size mismatch")
    backend = make_url(database_url).get_backend_name()
    if manifest.get("backend") != backend:
        raise ValueError("backup backend does not match restore target")
    return manifest


def safe_restore_database(
    database_url: str,
    source: str | Path,
    *,
    confirmation: str | None = None,
    dry_run: bool = False,
) -> dict:
    manifest = verify_backup(database_url, source)
    nonempty = _target_nonempty(database_url)
    if nonempty and confirmation != "RESTORE_OVER_EXISTING_DATA":
        raise ValueError(
            "restore target is non-empty; pass the explicit destructive confirmation"
        )
    report = {
        "format": "javes-restore-preflight.v1",
        "backend": manifest["backend"],
        "backup_sha256": manifest["sha256"],
        "backup_size_bytes": manifest["size_bytes"],
        "target_nonempty": nonempty,
        "dry_run": dry_run,
        "restored": False,
    }
    if not dry_run:
        restore_database(database_url, source)
        report["restored"] = True
    return report


def _database_invariants(database_url: str) -> dict:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected = {
            "cases",
            "jobs",
            "outbox",
            "case_events",
            "alembic_version",
        }
        missing = sorted(expected - tables)
        counts: dict[str, int] = {}
        with engine.connect() as connection:
            for table in sorted(expected & tables):
                counts[table] = int(
                    connection.execute(
                        text(f'SELECT COUNT(*) FROM "{table}"')
                    ).scalar_one()
                )
            migration = (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
                if "alembic_version" in tables
                else None
            )
        return {
            "missing_tables": missing,
            "row_counts": counts,
            "migration_revision": migration,
            "valid": not missing,
        }
    finally:
        engine.dispose()


def rehearse_restore(
    database_url: str,
    source: str | Path,
    report_path: str | Path,
) -> dict:
    if _target_nonempty(database_url):
        raise ValueError("isolated rehearsal target must be empty")
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    preflight = safe_restore_database(database_url, source)
    invariants = _database_invariants(database_url)
    completed_at = datetime.now(timezone.utc)
    manifest = verify_backup(database_url, source)
    created_at = datetime.fromisoformat(str(manifest["created_at"]))
    report = {
        "format": "javes-restore-rehearsal.v1",
        "status": "PASS" if invariants["valid"] else "FAIL",
        "backend": manifest["backend"],
        "backup_sha256": manifest["sha256"],
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "rpo_seconds": max((started_at - created_at).total_seconds(), 0.0),
        "rto_seconds": time.monotonic() - started,
        "preflight": preflight,
        "invariants": invariants,
    }
    target = Path(report_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not invariants["valid"]:
        raise RuntimeError("restored database failed application invariants")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup or restore the JAVES database")
    parser.add_argument(
        "action", choices=("backup", "restore", "preflight", "rehearse")
    )
    parser.add_argument("path")
    parser.add_argument("--database-url", default=os.environ.get("JAVES_DATABASE_URL"))
    parser.add_argument("--confirm-destructive")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    configure_telemetry("javes-main-backup")
    try:
        with operation(
            "database.recovery",
            {"component": "main", "operation": args.action},
        ):
            if args.action == "backup":
                backup_database(args.database_url, args.path)
            elif args.action == "restore":
                result = safe_restore_database(
                    args.database_url,
                    args.path,
                    confirmation=args.confirm_destructive,
                )
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.action == "preflight":
                result = safe_restore_database(
                    args.database_url, args.path, dry_run=True
                )
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                if not args.report:
                    parser.error("--report is required for rehearse")
                print(
                    json.dumps(
                        rehearse_restore(args.database_url, args.path, args.report),
                        indent=2,
                        sort_keys=True,
                    )
                )
    finally:
        shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

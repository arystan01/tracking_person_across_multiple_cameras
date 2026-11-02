"""Self-contained Alembic entry point for installed JAVES Main packages."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str) -> Config:
    migrations = Path(__file__).resolve().parent / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_database(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def current_revision(database_url: str) -> None:
    command.current(alembic_config(database_url), verbose=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the JAVES Main database schema")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("JAVES_DATABASE_URL"),
        help="SQLAlchemy URL; defaults to JAVES_DATABASE_URL",
    )
    parser.add_argument("action", choices=("upgrade", "current"))
    parser.add_argument("--revision", default="head")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    if args.action == "upgrade":
        upgrade_database(args.database_url, args.revision)
    else:
        current_revision(args.database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

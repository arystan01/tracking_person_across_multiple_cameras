"""Administrative CLI for bootstrapping the first internal ROOT account."""

from __future__ import annotations

import argparse
import getpass
import os
from typing import Sequence

from javes_main.adapters.system import ContentIdGenerator, SystemClock
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository

from .auth_service import AuthService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap JAVES internal IAM")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("JAVES_DATABASE_URL"),
        help="SQLAlchemy URL; defaults to JAVES_DATABASE_URL",
    )
    parser.add_argument("--username", default="root")
    parser.add_argument(
        "--password-env",
        default="JAVES_BOOTSTRAP_PASSWORD",
        help="environment variable containing the initial password",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    password = os.environ.get(args.password_env)
    if password is None:
        password = getpass.getpass("Initial ROOT password: ")

    repository = SqlAlchemyRepository.from_url(args.database_url)
    user = AuthService(
        repository=repository,
        clock=SystemClock(),
        ids=ContentIdGenerator(),
    ).bootstrap_root(args.username, password)
    print(f"Created ROOT {user.username!r} with user_id={user.user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

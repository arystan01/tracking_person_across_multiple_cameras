"""CLI for auditing/backfilling decision replay capability."""

from __future__ import annotations

import argparse
import json
import os

from javes_main.application.replay_audit import audit_replay_ledgers
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit immutable Routing evidence required for decision revoke"
    )
    parser.add_argument(
        "--database-url", default=os.environ.get("JAVES_DATABASE_URL")
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="backfill only cases whose persisted evidence is provably complete",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    repository = SqlAlchemyRepository.from_url(args.database_url)
    report = audit_replay_ledgers(repository, migrate=args.migrate)
    print(json.dumps(report, indent=2, sort_keys=True))
    if any(not item["complete"] for item in report):
        raise SystemExit(2)


if __name__ == "__main__":
    main()


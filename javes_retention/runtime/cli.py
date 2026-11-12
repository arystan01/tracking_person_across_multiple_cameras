"""Read-only Retention inventory/tombstone inspection CLI."""

from __future__ import annotations

import argparse
import json

from .repository import SQLiteRetentionRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a JAVES-AI Retention journal without deleting data"
    )
    parser.add_argument("database", help="path to retention SQLite database")
    parser.add_argument("--case-id", help="limit inventory/tombstones to one case")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    repository = SQLiteRetentionRepository(args.database)
    try:
        payload = {
            "resources": [
                {
                    "resource": item.resource.model_dump(mode="json"),
                    "state": item.state,
                    "attempt": item.attempt,
                    "last_error": item.last_error,
                    "deleted_at": (
                        item.deleted_at.isoformat() if item.deleted_at else None
                    ),
                    "proof": item.proof,
                }
                for item in repository.list_resources(args.case_id)
            ],
            "tombstones": repository.list_tombstones(args.case_id),
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    finally:
        repository.close()


if __name__ == "__main__":
    main()

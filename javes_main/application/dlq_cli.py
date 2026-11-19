from __future__ import annotations

import argparse
import json
from typing import Sequence

from javes_main.adapters.system import SystemClock
from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from javes_main.settings import MainSettings

from .dlq import DeadLetterService


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or replay JAVES dead letters")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    replay = sub.add_parser("replay")
    replay.add_argument("kind", choices=("job", "outbox", "routing"))
    replay.add_argument("id")
    args = parser.parse_args(argv)
    settings = MainSettings.from_env()
    service = DeadLetterService(
        SqlAlchemyRepository.from_url(settings.database_url), SystemClock()
    )
    result = service.list() if args.action == "list" else service.replay(args.kind, args.id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

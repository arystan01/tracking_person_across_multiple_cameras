from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from javes_main.persistence.sqlalchemy import SqlAlchemyRepository
from javes_telemetry import configure_telemetry, operation, shutdown_telemetry


def _hash_id(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a secret/path-free incident state snapshot"
    )
    parser.add_argument("--database-url", default=os.environ.get("JAVES_DATABASE_URL"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or JAVES_DATABASE_URL is required")
    configure_telemetry("javes-main-incident-snapshot")
    try:
        repository = SqlAlchemyRepository.from_url(args.database_url)
        with operation(
            "incident.snapshot",
            {"component": "main", "operation": "incident_snapshot"},
        ):
            cases = repository.list_cases()
            jobs = [
                job
                for case in cases
                for job in repository.list_jobs(case.request.case_id)
            ]
            outbox = repository.list_outbox()
            events = []
            after = None
            while True:
                page = repository.list_audit_events(after=after, limit=1000)
                events.extend(page)
                if len(page) < 1000:
                    break
                last = page[-1]
                after = (last.occurred_at, last.event_id)
            payload = {
                "format": "javes-incident-snapshot.v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "case_count": len(cases),
                "case_ids": sorted(_hash_id(case.request.case_id) for case in cases),
                "lifecycle_counts": {
                    lifecycle: sum(case.lifecycle.value == lifecycle for case in cases)
                    for lifecycle in sorted({case.lifecycle.value for case in cases})
                },
                "job_state_counts": {
                    state: sum(job.state.value == state for job in jobs)
                    for state in sorted({job.state.value for job in jobs})
                },
                "outbox_state_counts": {
                    state: sum(item.state.value == state for item in outbox)
                    for state in sorted({item.state.value for item in outbox})
                },
                "audit_event_count": len(events),
                "last_audit_event_id": (
                    _hash_id(events[-1].event_id) if events else None
                ),
            }
            target = Path(args.output).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps({"sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            )
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

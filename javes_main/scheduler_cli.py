from __future__ import annotations

import argparse
import time

from .domain import CaseLifecycle
from .production import build_api_first_system
from .settings import MainSettings


def run_scheduler_once(system) -> dict[str, int]:
    manager = system.manager
    now = manager.clock.now()
    heartbeats = 0
    for case in manager.repository.list_cases(
        lifecycles={CaseLifecycle.SEARCHING.value}
    ):
        if case.next_heartbeat_at is not None and case.next_heartbeat_at <= now:
            manager.heartbeat(case.request.case_id)
            heartbeats += 1
    retention_expiries = manager.enqueue_due_retention()
    routing = system.routing_dispatcher.run_once(limit=100)
    return {
        "heartbeats": heartbeats,
        "retention_expiries": retention_expiries,
        "routing_completed": routing.completed,
        "routing_failed": routing.failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Main scheduler")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = MainSettings.from_env()
    system = build_api_first_system(settings)
    while True:
        run_scheduler_once(system)
        if args.once:
            return
        time.sleep(settings.scheduler_poll_s)


if __name__ == "__main__":
    main()

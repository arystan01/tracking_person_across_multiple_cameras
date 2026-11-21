from __future__ import annotations

import argparse
import os
import time

from javes_main.adapters.artifacts import FileArtifactStore
from javes_workers import HttpWorkerClient
from javes_telemetry import configure_telemetry, shutdown_telemetry

from .factory import build_local_retention_worker
from .runner import RetentionTaskRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Retention worker")
    parser.add_argument("--main-url", required=True)
    parser.add_argument(
        "--token", default=os.environ.get("JAVES_RETENTION_WORKER_TOKEN")
    )
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_RETENTION_WORKER_TOKEN is required")
    configure_telemetry("javes-retention-worker")

    worker = build_local_retention_worker(
        database_path=args.state_db,
        artifact_store=FileArtifactStore(args.artifact_root),
    )
    client = HttpWorkerClient(args.main_url, "retention", args.token, args.worker_id)
    runner = RetentionTaskRunner(worker, client)
    try:
        while True:
            processed = runner.run_once()
            if args.once:
                break
            if not processed:
                time.sleep(args.poll_seconds)
    finally:
        worker.close()
        shutdown_telemetry()


if __name__ == "__main__":
    main()

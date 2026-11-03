from __future__ import annotations

import argparse
import time

from javes_main.adapters.artifacts import FileArtifactStore
from javes_workers import HttpWorkerClient

from .factory import build_local_retention_worker
from .runtime import RetentionTaskRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Retention worker")
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    worker = build_local_retention_worker(
        database_path=args.state_db,
        artifact_store=FileArtifactStore(args.artifact_root),
    )
    client = HttpWorkerClient(
        args.main_url, "retention", args.token, args.worker_id
    )
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


if __name__ == "__main__":
    main()

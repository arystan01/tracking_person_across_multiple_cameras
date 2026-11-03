from __future__ import annotations

import argparse
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Perception worker")
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--archive-url")
    parser.add_argument("--archive-token")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    from javes_main.adapters.artifacts import FileArtifactStore
    from javes_workers import HttpWorkerClient

    from .archive_api import HttpVideoArchive
    from .config import load_settings
    from .runtime import PerceptionTaskRunner
    from .state import PerceptionStateStore
    from .worker import PerceptionWorker

    state = PerceptionStateStore(args.state_db)
    artifacts = FileArtifactStore(args.artifact_root)
    settings = load_settings(args.config)
    archive = None
    if bool(args.archive_url) != bool(args.archive_token):
        parser.error("--archive-url and --archive-token must be provided together")
    if args.archive_url:
        archive = HttpVideoArchive(
            args.archive_url,
            args.archive_token,
            camera_ids=set(settings.cameras),
            artifact_root=args.artifact_root,
        )
    worker = PerceptionWorker(
        settings, artifacts, archive=archive, state_store=state
    )
    client = HttpWorkerClient(
        args.main_url, "perception", args.token, args.worker_id
    )
    runner = PerceptionTaskRunner(worker, client, state)
    try:
        while True:
            processed = runner.run_once()
            if args.once:
                break
            time.sleep(args.poll_seconds)
    finally:
        state.close()


if __name__ == "__main__":
    main()

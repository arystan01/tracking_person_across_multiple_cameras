from __future__ import annotations

import argparse
import logging
import os
import time

from javes_workers import HttpWorkerClient
from javes_telemetry import configure_telemetry, shutdown_telemetry

from .archive import HttpClipArchive
from .runner import ClipTaskRunner
from .storage import ClipStore
from .worker import ClipWorker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the real JAVES-AI Clip worker (requires FFmpeg)"
    )
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--token", default=os.environ.get("JAVES_CLIP_WORKER_TOKEN"))
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--archive-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--archive-url", required=True)
    parser.add_argument(
        "--archive-token", default=os.environ.get("JAVES_ARCHIVE_SERVICE_TOKEN")
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_CLIP_WORKER_TOKEN is required")
    if not args.archive_token:
        parser.error("--archive-token or JAVES_ARCHIVE_SERVICE_TOKEN is required")
    if args.poll_seconds < 0:
        parser.error("--poll-seconds must not be negative")
    logging.basicConfig(level=logging.INFO)
    configure_telemetry("javes-clip-worker")
    client = HttpWorkerClient(args.main_url, "clip", args.token, args.worker_id)
    store = ClipStore(args.state_db, args.artifact_root)
    archive = HttpClipArchive(
        args.archive_url,
        args.archive_token,
        archive_root=args.archive_root,
    )
    runner = ClipTaskRunner(ClipWorker(store, archive), client)
    try:
        while True:
            try:
                processed = runner.run_once()
            except Exception:
                logging.exception('Could not claim Clip tasks')
                if args.once:
                    raise
                processed = 0
            if args.once:
                return
            if not processed:
                time.sleep(args.poll_seconds)
    finally:
        store.close()
        archive.client.close()
        shutdown_telemetry()


if __name__ == "__main__":
    main()

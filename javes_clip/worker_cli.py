from __future__ import annotations

import argparse
import os
import time

from javes_workers import HttpWorkerClient
from javes_telemetry import configure_telemetry, shutdown_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report Clip operations unavailable until the real Clip worker is installed"
    )
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--token", default=os.environ.get("JAVES_CLIP_WORKER_TOKEN"))
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_CLIP_WORKER_TOKEN is required")
    configure_telemetry("javes-clip-worker-unavailable")
    client = HttpWorkerClient(args.main_url, "clip", args.token, args.worker_id)
    while True:
        tasks = client.claim(limit=10, lease_seconds=30)
        for task in tasks:
            client.fail(
                task,
                error_code="CLIP_WORKER_UNAVAILABLE",
                detail="real Clip media adapter is not installed",
                retryable=False,
            )
        if args.once:
            shutdown_telemetry()
            return
        if not tasks:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()

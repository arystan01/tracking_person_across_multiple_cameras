"""CLI entrypoints for the NVIDIA Archive service and safe inspection."""

from __future__ import annotations

import argparse
import json
import os

from .api import create_nvidia_archive_app
from .registry import NvidiaArchiveRegistry
from javes_telemetry import configure_telemetry, shutdown_telemetry


def api_main() -> None:
    parser = argparse.ArgumentParser(description="Run the NVIDIA Archive API")
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument(
        "--token", default=os.environ.get("JAVES_ARCHIVE_SERVICE_TOKEN")
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_ARCHIVE_SERVICE_TOKEN is required")
    configure_telemetry("javes-nvidia-archive-api")
    registry = NvidiaArchiveRegistry(
        args.root,
        args.manifest,
        verify=args.verify,
    )
    import uvicorn

    try:
        uvicorn.run(
            create_nvidia_archive_app(registry, service_token=args.token),
            host=args.host,
            port=args.port,
        )
    finally:
        shutdown_telemetry()


def inspect_main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify and inspect NVIDIA Archive metadata"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    registry = NvidiaArchiveRegistry(
        args.root,
        args.manifest,
        verify=args.verify,
    )
    print(json.dumps(registry.inspect(), indent=2, sort_keys=True))


if __name__ == "__main__":
    inspect_main()

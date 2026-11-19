"""CLI entrypoints for the NVIDIA Archive service and safe inspection."""

from __future__ import annotations

import argparse
import json

from .api import create_nvidia_archive_app
from .registry import NvidiaArchiveRegistry


def api_main() -> None:
    parser = argparse.ArgumentParser(description="Run the NVIDIA Archive API")
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--token", required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args()
    registry = NvidiaArchiveRegistry(
        args.root,
        args.manifest,
        verify=args.verify,
    )
    import uvicorn

    uvicorn.run(
        create_nvidia_archive_app(registry, service_token=args.token),
        host=args.host,
        port=args.port,
    )


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

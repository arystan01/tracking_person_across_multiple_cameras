from __future__ import annotations

import argparse
import os

from .api import create_site_config_app
from .registry import FilesystemSiteConfigRegistry
from javes_telemetry import configure_telemetry, shutdown_telemetry
from javes_security import RotationWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Site Config API")
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--token", default=os.environ.get("JAVES_SITE_CONFIG_SERVICE_TOKEN")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_SITE_CONFIG_SERVICE_TOKEN is required")
    rotation = RotationWindow.from_strings(
        args.token,
        os.environ.get("JAVES_SITE_CONFIG_SERVICE_TOKEN_PREVIOUS"),
        os.environ.get("JAVES_SITE_CONFIG_SERVICE_TOKEN_PREVIOUS_VALID_UNTIL"),
    )
    rotation.require_ready()
    configure_telemetry("javes-site-config-api")
    import uvicorn

    try:
        uvicorn.run(
            create_site_config_app(
                FilesystemSiteConfigRegistry(args.root),
                service_token=rotation,
            ),
            host=args.host,
            port=args.port,
        )
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

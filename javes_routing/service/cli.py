from __future__ import annotations

import argparse
import os

from javes_site_config import HttpSiteConfigClient

from .api import create_routing_app
from javes_telemetry import configure_telemetry, shutdown_telemetry
from javes_security import RotationWindow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Routing API")
    parser.add_argument("--site-config-url", required=True)
    parser.add_argument(
        "--site-config-token", default=os.environ.get("JAVES_SITE_CONFIG_SERVICE_TOKEN")
    )
    parser.add_argument(
        "--token", default=os.environ.get("JAVES_ROUTING_SERVICE_TOKEN")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if not args.site_config_token or not args.token:
        parser.error("service tokens are required through arguments or environment")
    rotation = RotationWindow.from_strings(
        args.token,
        os.environ.get("JAVES_ROUTING_SERVICE_TOKEN_PREVIOUS"),
        os.environ.get("JAVES_ROUTING_SERVICE_TOKEN_PREVIOUS_VALID_UNTIL"),
    )
    rotation.require_ready()
    configure_telemetry("javes-routing-api")
    import uvicorn

    sites = HttpSiteConfigClient(args.site_config_url, args.site_config_token)
    try:
        uvicorn.run(
            create_routing_app(sites, service_token=rotation),
            host=args.host,
            port=args.port,
        )
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

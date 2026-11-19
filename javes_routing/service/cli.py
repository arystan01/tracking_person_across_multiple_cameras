from __future__ import annotations

import argparse

from javes_site_config import HttpSiteConfigClient

from .api import create_routing_app
from javes_telemetry import configure_telemetry, shutdown_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Routing API")
    parser.add_argument("--site-config-url", required=True)
    parser.add_argument("--site-config-token", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    configure_telemetry("javes-routing-api")
    import uvicorn

    sites = HttpSiteConfigClient(args.site_config_url, args.site_config_token)
    try:
        uvicorn.run(
            create_routing_app(sites, service_token=args.token),
            host=args.host,
            port=args.port,
        )
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

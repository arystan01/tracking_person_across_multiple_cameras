from __future__ import annotations

import argparse

from javes_telemetry import configure_telemetry, shutdown_telemetry

from .app import create_dashboard_app
from .settings import DashboardSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI React Dashboard BFF")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    settings = DashboardSettings.from_env()
    configure_telemetry("javes-dashboard")
    app = create_dashboard_app(settings)
    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

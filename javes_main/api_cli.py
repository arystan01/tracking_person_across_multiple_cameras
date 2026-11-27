from __future__ import annotations

import argparse

from .api import create_app
from .production import build_api_first_system, rotation_status, worker_tokens
from .settings import MainSettings
from .observability import configure_json_logging
from javes_telemetry import configure_telemetry, shutdown_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Main/Controls API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    settings = MainSettings.from_env()
    configure_telemetry("javes-main-api")
    configure_json_logging(settings.log_level)
    system = build_api_first_system(settings)
    app = create_app(
        system.manager,
        auth=system.auth,
        worker_tokens=worker_tokens(settings),
        worker_lease_secret=settings.worker_lease_secret.get_secret_value(),  # type: ignore[union-attr]
        worker_lease_secret_previous=(
            settings.worker_lease_secret_previous.get_secret_value()
            if settings.worker_lease_secret_previous is not None
            else None
        ),
        worker_lease_secret_previous_valid_until=settings.worker_lease_secret_previous_valid_until,
        require_https=settings.require_https,
        trusted_proxies=settings.trusted_proxy_cidrs,
        cors_origins=settings.cors_origins,
        allowed_site_versions=[
            tuple(item.split("@", 1)) for item in settings.allowed_site_versions
        ],
        rotation_status=lambda: rotation_status(settings),
    )
    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()

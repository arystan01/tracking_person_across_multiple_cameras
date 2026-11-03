from __future__ import annotations

import argparse

from .api import create_app
from .production import build_api_first_system, worker_tokens
from .settings import MainSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Main/Controls API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    settings = MainSettings.from_env()
    system = build_api_first_system(settings)
    app = create_app(
        system.manager,
        auth=system.auth,
        worker_tokens=worker_tokens(settings),
        worker_lease_secret=settings.worker_lease_secret.get_secret_value(),  # type: ignore[union-attr]
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

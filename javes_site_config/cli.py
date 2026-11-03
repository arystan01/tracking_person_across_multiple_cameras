from __future__ import annotations

import argparse

from .api import create_site_config_app
from .registry import FilesystemSiteConfigRegistry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JAVES-AI Site Config API")
    parser.add_argument("--root", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        create_site_config_app(
            FilesystemSiteConfigRegistry(args.root), service_token=args.token
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()

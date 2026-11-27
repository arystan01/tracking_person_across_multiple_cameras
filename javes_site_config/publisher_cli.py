from __future__ import annotations

import argparse
import json

from .publisher import preview_params, preview_site, publish_params, publish_site


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or atomically publish immutable Site Config bundles"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preview-site", "publish-site"):
        child = subparsers.add_parser(command)
        child.add_argument("--source", required=True)
        if command == "publish-site":
            child.add_argument("--root", required=True)
    for command in ("preview-params", "publish-params"):
        child = subparsers.add_parser(command)
        child.add_argument("--source", required=True)
        if command == "publish-params":
            child.add_argument("--root", required=True)
    args = parser.parse_args()
    if args.command == "preview-site":
        result = preview_site(args.source)
    elif args.command == "publish-site":
        result = publish_site(args.source, args.root)
    elif args.command == "preview-params":
        result = preview_params(args.source)
    else:
        result = publish_params(args.source, args.root)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

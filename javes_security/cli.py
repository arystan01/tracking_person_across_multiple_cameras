from __future__ import annotations

import argparse
import json
import os

from .rotation import RotationWindow, secret_fingerprint


PREFIXES = {
    "routing": "JAVES_ROUTING_SERVICE_TOKEN",
    "site-config": "JAVES_SITE_CONFIG_SERVICE_TOKEN",
    "archive": "JAVES_ARCHIVE_SERVICE_TOKEN",
    "perception": "JAVES_PERCEPTION_WORKER_TOKEN",
    "clip": "JAVES_CLIP_WORKER_TOKEN",
    "retention": "JAVES_RETENTION_WORKER_TOKEN",
    "lease": "JAVES_WORKER_LEASE_SECRET",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate prepare/switch/verify/retire service-token rotation state"
    )
    parser.add_argument("phase", choices=("prepare", "switch", "verify", "retire"))
    parser.add_argument("component", choices=sorted(PREFIXES))
    args = parser.parse_args()
    prefix = PREFIXES[args.component]
    current = os.environ.get(prefix)
    previous = os.environ.get(prefix + "_PREVIOUS")
    deadline = os.environ.get(prefix + "_PREVIOUS_VALID_UNTIL")
    if not current:
        parser.error(f"{prefix} is required")
    if args.phase in {"switch", "verify"}:
        rotation = RotationWindow.from_strings(current, previous, deadline)
        rotation.require_ready()
        status = rotation.status()
    elif args.phase == "retire":
        if previous or deadline:
            parser.error(
                "retire is complete only after previous secret and deadline are removed"
            )
        status = RotationWindow(current).status()
    else:
        if previous:
            parser.error("prepare starts before a previous secret is configured")
        status = {
            "current_key_id": secret_fingerprint(current),
            "ready_for_new_secret": True,
        }
    print(
        json.dumps(
            {"component": args.component, "phase": args.phase, **status},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

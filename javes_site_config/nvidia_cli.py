from __future__ import annotations

import argparse
import json
from pathlib import Path

from .nvidia import (
    SITE_ID,
    SITE_VERSION,
    calibration_checksums,
    canonical_site,
    write_site_registry,
)
from .registry import FilesystemSiteConfigRegistry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap or verify the canonical NVIDIA 12-camera Site Config"
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--registry-root", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    repository_root = Path(args.repository_root).resolve(strict=True)
    registry_root = Path(args.registry_root).resolve()
    target = registry_root / "sites" / SITE_ID / SITE_VERSION
    if not args.verify_only:
        if target.exists():
            raise FileExistsError(
                "immutable NVIDIA Site Config already exists; use --verify-only"
            )
        write_site_registry(registry_root, repository_root)
    registry = FilesystemSiteConfigRegistry(registry_root)
    snapshot = registry.site(SITE_ID, SITE_VERSION)
    expected = canonical_site(repository_root)
    result = {
        "format": "javes-nvidia-site-bootstrap.v1",
        "site_id": SITE_ID,
        "version": SITE_VERSION,
        "checksum": snapshot.checksum,
        "cameras": len(snapshot.graph.cameras),
        "calibration_checksums": calibration_checksums(expected),
        "verified": len(snapshot.graph.cameras) == 12,
    }
    if not result["verified"]:
        raise RuntimeError("NVIDIA Site Config verification failed")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

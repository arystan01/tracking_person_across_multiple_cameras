"""Immutable development/holdout report that never tunes parameters."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence


def build_shadow_report(
    manifest_bytes: bytes,
    evaluation_bytes: bytes,
    params_bytes: bytes,
) -> dict:
    manifest = json.loads(manifest_bytes)
    evaluation = json.loads(evaluation_bytes)
    split_by_id = {
        item["id"]: item["split"]
        for item in manifest.get("scenarios", [])
        if not item.get("quarantined")
    }
    evaluated = {item["scenario_id"]: item for item in evaluation.get("scenarios", [])}
    if set(evaluated) != set(split_by_id):
        missing = sorted(set(split_by_id) - set(evaluated))
        extra = sorted(set(evaluated) - set(split_by_id))
        raise ValueError(f"shadow evaluation scenario mismatch: missing={missing}, extra={extra}")
    splits = {}
    for split in ("development", "holdout"):
        items = [evaluated[key] for key, value in split_by_id.items() if value == split]
        splits[split] = {
            "scenario_ids": sorted(item["scenario_id"] for item in items),
            "passed": sum(bool(item.get("passed")) for item in items),
            "total": len(items),
            "pass_rate": sum(bool(item.get("passed")) for item in items) / len(items),
        }
    return {
        "format": "javes-routing-shadow.v1",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "params_sha256": hashlib.sha256(params_bytes).hexdigest(),
        "evaluation_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
        "params_mutated": False,
        "splits": splits,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build immutable NVIDIA holdout shadow report")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--params", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = build_shadow_report(
        Path(args.manifest).read_bytes(),
        Path(args.evaluation).read_bytes(),
        Path(args.params).read_bytes(),
    )
    Path(args.out).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the deterministic stable v1 schema/golden handoff bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from . import CONTRACTS_VERSION
from .catalog import inventory_document
from .compatibility import compatibility_issues
from .conventions import conventions_document
from .golden import GOLDEN_FIXTURES, canonical_golden_documents
from .schema import PUBLIC_MODELS
from pydantic import ValidationError


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def build_bundle(output: str | Path) -> list[Path]:
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    documents: dict[str, object] = {
        "inventory.json": inventory_document(),
        "conventions.json": conventions_document(),
    }
    for name, model in sorted(PUBLIC_MODELS.items()):
        documents[f"schemas/{name}.schema.json"] = model.model_json_schema(
            by_alias=True
        )
    documents.update(
        {
            f"golden/{name}": payload
            for name, payload in canonical_golden_documents().items()
        }
    )
    written: list[Path] = []
    hashes: dict[str, str] = {}
    for relative, value in sorted(documents.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _json_bytes(value)
        path.write_bytes(data)
        written.append(path)
        hashes[relative] = hashlib.sha256(data).hexdigest()
    manifest = {
        "format": "javes-contract-bundle.v1",
        "boundary_version": CONTRACTS_VERSION,
        "release_status": "STABLE",
        "fixture_count": len(GOLDEN_FIXTURES),
        "schema_count": len(PUBLIC_MODELS),
        "files": hashes,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return [manifest_path, *written]


def check_baseline(baseline: str | Path) -> list[str]:
    root = Path(baseline)
    issues: list[str] = []
    schema_root = root / "schemas"
    for path in sorted(schema_root.glob("*.schema.json")):
        name = path.name.removesuffix(".schema.json")
        model = PUBLIC_MODELS.get(name)
        if model is None:
            issues.append(f"public model removed: {name}")
            continue
        old = json.loads(path.read_text(encoding="utf-8"))
        issues.extend(
            f"{name}: {issue}"
            for issue in compatibility_issues(
                old, model.model_json_schema(by_alias=True)
            )
        )
    inventory_path = root / "inventory.json"
    if not inventory_path.is_file():
        issues.append("missing baseline inventory.json")
    else:
        old_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        new_inventory = inventory_document()
        new_endpoints = {
            (
                item["owner"],
                item["consumer"],
                item["method"],
                item["path"],
                item.get("request"),
                item.get("response"),
            )
            for item in new_inventory["endpoints"]
        }
        for item in old_inventory.get("endpoints", []):
            signature = (
                item["owner"],
                item["consumer"],
                item["method"],
                item["path"],
                item.get("request"),
                item.get("response"),
            )
            if item["path"] == "/v1/cases/{case_id}/commands/{action}":
                expanded = {
                    (
                        item["owner"],
                        item["consumer"],
                        item["method"],
                        f"/v1/cases/{{case_id}}/commands/{action}",
                        item.get("request"),
                        item.get("response"),
                    )
                    for action in ("close", "export", "delete")
                }
                if expanded <= new_endpoints:
                    continue
            if signature not in new_endpoints:
                issues.append(
                    f"endpoint removed or changed: {item['method']} {item['path']}"
                )
    golden_root = root / "golden"
    for old_path in sorted(golden_root.rglob("*.json")):
        relative = old_path.relative_to(golden_root).as_posix()
        fixture = GOLDEN_FIXTURES.get(relative)
        if fixture is None:
            issues.append(f"golden fixture removed: {relative}")
            continue
        model, _ = fixture
        try:
            model.model_validate_json(old_path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            issues.append(f"golden/{relative}: no longer validates: {exc}")
    return issues


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build JAVES boundary handoff bundle")
    parser.add_argument("--out", default="contract-bundle")
    parser.add_argument("--check-baseline")
    args = parser.parse_args(argv)
    if args.check_baseline:
        issues = check_baseline(args.check_baseline)
        if issues:
            for issue in issues:
                print(issue)
            return 1
        return 0
    for path in build_bundle(args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

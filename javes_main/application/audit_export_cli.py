from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import httpx

from javes_contracts import AuditExportRequest
from javes_telemetry import configure_telemetry, operation, shutdown_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and download an authenticated JAVES audit export"
    )
    parser.add_argument("--main-url", required=True)
    parser.add_argument("--token", default=os.environ.get("JAVES_AUDIT_EXPORT_TOKEN"))
    parser.add_argument("--export-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--from-time")
    parser.add_argument("--to-time")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--actor-id", action="append", default=[])
    parser.add_argument("--event-type", action="append", default=[])
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or JAVES_AUDIT_EXPORT_TOKEN is required")
    configure_telemetry("javes-main-audit-export")
    request = AuditExportRequest.model_validate(
        {
            "export_id": args.export_id,
            "from_time": args.from_time,
            "to_time": args.to_time,
            "case_ids": args.case_id,
            "actor_ids": args.actor_id,
            "event_types": args.event_type,
        }
    )
    headers = {"authorization": f"Bearer {args.token}"}
    base = args.main_url.rstrip("/")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    events_path = output / f"{args.export_id}.ndjson"
    manifest_path = output / f"{args.export_id}.manifest.json"
    partial_path = output / f".{args.export_id}.ndjson.partial"
    try:
        with operation(
            "audit.export.client", {"component": "main", "operation": "audit_export"}
        ):
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    f"{base}/v1/audit/exports",
                    headers=headers,
                    json=request.model_dump(mode="json"),
                )
                response.raise_for_status()
                manifest = response.json()
                digest = hashlib.sha256()
                size = 0
                with client.stream(
                    "GET", f"{base}{manifest['events_url']}", headers=headers
                ) as events:
                    events.raise_for_status()
                    with partial_path.open("wb") as destination:
                        for chunk in events.iter_bytes():
                            destination.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                if size != manifest["size_bytes"]:
                    raise ValueError(
                        "downloaded audit export size does not match manifest"
                    )
                if digest.hexdigest() != manifest["sha256"]:
                    raise ValueError(
                        "downloaded audit export checksum does not match manifest"
                    )
                os.replace(partial_path, events_path)
    finally:
        partial_path.unlink(missing_ok=True)
        shutdown_telemetry()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"events": str(events_path), "manifest": str(manifest_path)}))

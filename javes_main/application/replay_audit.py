"""Audit and conservative backfill of immutable Routing replay ledgers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from javes_main.domain.models import CaseDataKind, CaseDataRecord, CaseRecord
from javes_main.ports import CaseRepository


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def audit_case_replay_ledger(
    repository: CaseRepository,
    case: CaseRecord,
    *,
    migrate: bool = False,
) -> dict[str, Any]:
    case_id = case.request.case_id
    runs = [
        item
        for item in repository.list_case_data(
            case_id, kinds={CaseDataKind.ROUTING_RUN.value}
        )
        if item.payload.get("trigger_type") != "DECISION_REVOKE"
    ]
    inputs = repository.list_case_data(
        case_id, kinds={CaseDataKind.ROUTING_INPUT.value}
    )
    input_ids = {item.record_id for item in inputs}
    errors: list[str] = []
    candidates: list[CaseDataRecord] = []
    for sequence, run in enumerate(sorted(runs, key=lambda item: (item.occurred_at, item.record_id))):
        if run.record_id in input_ids:
            continue
        try:
            trigger = repository.get_routing_trigger(run.record_id)
        except KeyError:
            errors.append(f"missing routing trigger {run.record_id}")
            continue
        if trigger.state.value != "DONE":
            errors.append(f"routing trigger {run.record_id} is not DONE")
            continue
        anchors = run.payload.get("anchors")
        if not isinstance(anchors, dict):
            errors.append(f"routing run {run.record_id} has no effective anchors")
            continue
        source_decision_id = None
        marker = ":decision:"
        if trigger.trigger_type == "OPERATOR_DECISION" and marker in trigger.dedupe_key:
            source_decision_id = trigger.dedupe_key.rsplit(marker, 1)[1]
        payload = {
            "sequence": sequence,
            "trigger_id": run.record_id,
            "trigger_type": trigger.trigger_type,
            "trigger_dedupe_key": trigger.dedupe_key,
            "source_decision_id": source_decision_id,
            "effective_now": run.occurred_at.isoformat(),
            "site_id": case.request.site_id,
            "site_version": case.request.site_version,
            "params_version": case.params_version,
            "contracts_version": "javes-boundary.v1alpha1",
            "input_case_revision": run.payload.get("input_case_revision"),
            "output_case_revision": run.payload.get("output_case_revision"),
            "output_route_revision": run.payload.get("route_revision"),
            "probe_results": [
                item.model_dump(mode="json", by_alias=True)
                for item in trigger.probe_results
            ],
            "anchors": anchors,
            "active_decision_ids": [],
        }
        payload["fingerprint"] = _fingerprint(payload)
        candidates.append(
            CaseDataRecord(
                record_id=run.record_id,
                case_id=case_id,
                kind=CaseDataKind.ROUTING_INPUT,
                schema_version="routing-input.v1",
                occurred_at=run.occurred_at,
                payload=payload,
            )
        )

    complete = not errors and len(inputs) + len(candidates) == len(runs) and bool(runs)
    migrated = 0
    if migrate and complete and candidates:
        with repository.transaction():
            for record in candidates:
                repository.add_case_data(record)
                migrated += 1
            current = repository.get(case_id)
            expected = current.revision
            current.replay_capability = "COMPLETE"
            repository.save(current, expected)
    elif migrate and not complete:
        current = repository.get(case_id)
        if current.replay_capability != "INCOMPLETE":
            with repository.transaction():
                expected = current.revision
                current.replay_capability = "INCOMPLETE"
                repository.save(current, expected)
    return {
        "case_id": case_id,
        "complete": complete,
        "routing_runs": len(runs),
        "routing_inputs": len(inputs),
        "backfillable": len(candidates),
        "migrated": migrated,
        "errors": errors,
    }


def audit_replay_ledgers(
    repository: CaseRepository, *, migrate: bool = False
) -> list[dict[str, Any]]:
    return [
        audit_case_replay_ledger(repository, case, migrate=migrate)
        for case in repository.list_cases()
    ]


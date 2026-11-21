from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from javes_contracts import CONTRACTS_VERSION, CreateCaseBody, ProblemDetails
from javes_contracts.bundle import build_bundle, check_baseline
from javes_contracts.catalog import MODEL_OWNERS, inventory_document
from javes_contracts.compatibility import compatibility_issues
from javes_contracts.conventions import classify_problem, conventions_document
from javes_contracts.golden import GOLDEN_FIXTURES, canonical_golden_documents
from javes_contracts.schema import PUBLIC_MODELS
from javes_main.api import create_app
from tests.test_controls_api import make_system


def _tree(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_contract_bundle_is_deterministic_and_self_describing(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    build_bundle(first)
    build_bundle(second)
    assert _tree(first) == _tree(second)
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["boundary_version"] == CONTRACTS_VERSION
    assert manifest["release_status"] == "BLOCKED"
    assert manifest["schema_count"] == len(PUBLIC_MODELS)
    assert manifest["fixture_count"] == len(GOLDEN_FIXTURES)
    assert "manifest.json" not in manifest["files"]


def test_every_golden_fixture_validates_and_round_trips_canonically() -> None:
    documents = canonical_golden_documents()
    assert {
        "controls",
        "routing",
        "workers",
        "perception",
        "retention",
        "site",
        "archive",
    }.issubset({path.split("/", 1)[0] for path in documents})
    for path, (model, _) in GOLDEN_FIXTURES.items():
        parsed = model.model_validate(documents[path])
        assert (
            parsed.model_dump(mode="json", by_alias=True, exclude_none=False)
            == documents[path]
        )


def test_inventory_names_only_public_models_or_explicit_binary_responses() -> None:
    inventory = inventory_document()
    assert inventory["boundary_version"] == CONTRACTS_VERSION
    for models in MODEL_OWNERS.values():
        assert set(models).issubset(PUBLIC_MODELS)
    for endpoint in inventory["endpoints"]:
        assert endpoint["path"].startswith("/")
        assert endpoint["owner"]
        assert endpoint["consumer"]


def test_controls_catalog_paths_exist_in_real_openapi() -> None:
    system = make_system()
    deployed_paths = set(
        create_app(system.manager, auth=system.auth).openapi()["paths"]
    )
    catalog_paths = {
        endpoint["path"]
        for endpoint in inventory_document()["endpoints"]
        if endpoint["owner"] == "Controls"
    }
    assert catalog_paths <= deployed_paths


def test_archive_inventory_matches_the_deployed_read_only_api() -> None:
    endpoints = {
        (endpoint["method"], endpoint["path"])
        for endpoint in inventory_document()["endpoints"]
        if endpoint["owner"] == "Archive"
    }
    assert endpoints == {
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/internal/v1/archive/inspect"),
        ("POST", "/v1/video-segments"),
    }


def test_alpha_compatibility_allows_optional_addition_but_rejects_breaking_changes() -> (
    None
):
    old = {
        "type": "object",
        "properties": {"case_id": {"type": "string"}, "score": {"type": "number"}},
        "required": ["case_id"],
    }
    additive = deepcopy(old)
    additive["properties"]["note"] = {"type": ["string", "null"]}
    assert compatibility_issues(old, additive) == []

    removed = deepcopy(old)
    del removed["properties"]["score"]
    assert any(
        "property removed" in issue for issue in compatibility_issues(old, removed)
    )
    narrowed = deepcopy(old)
    narrowed["required"].append("score")
    assert any(
        "newly required" in issue for issue in compatibility_issues(old, narrowed)
    )


def test_generated_bundle_can_be_used_as_compatibility_baseline(tmp_path) -> None:
    root = tmp_path / "baseline"
    build_bundle(root)
    assert check_baseline(root) == []


def test_baseline_check_detects_endpoint_and_golden_breakage(tmp_path) -> None:
    root = tmp_path / "baseline"
    build_bundle(root)
    inventory_path = root / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["endpoints"].append(
        {
            "owner": "Controls",
            "consumer": "Dashboard",
            "method": "POST",
            "path": "/v1/removed",
            "request": None,
            "response": "CaseView",
        }
    )
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    create_case = root / "golden" / "controls" / "create-case.json"
    payload = json.loads(create_case.read_text(encoding="utf-8"))
    del payload["case_id"]
    create_case.write_text(json.dumps(payload), encoding="utf-8")
    issues = check_baseline(root)
    assert any("endpoint removed or changed" in issue for issue in issues)
    assert any("create-case.json" in issue for issue in issues)


def test_problem_retry_taxonomy_is_machine_readable() -> None:
    assert classify_problem("revision-conflict").retryable is True
    assert classify_problem("worker-lease").consumer_action == "reclaim"
    assert classify_problem("validation").retryable is False
    assert classify_problem("vendor-specific", 503).retryable is True
    conventions = conventions_document()
    assert conventions["release_status"] == "BLOCKED"
    assert "dashboard-contract-tests" in conventions["release_blockers"]
    assert "clip-worker-contract-tests" in conventions["release_blockers"]
    assert "operator-decision-revoke-semantics" not in conventions["release_blockers"]
    assert "legal-hold-authority" not in conventions["release_blockers"]
    ProblemDetails.model_validate(
        canonical_golden_documents()["controls/problem-retryable.json"]
    )


def test_new_platform_contracts_are_published_with_golden_examples() -> None:
    expected_models = {
        "AuditExportManifest",
        "AuditExportRequest",
        "CreateLegalHoldBody",
        "DecisionRecordView",
        "EvidenceBundleVerification",
        "EvidenceRef",
        "LegalHoldView",
        "NvidiaReleaseEvidence",
        "NvidiaReleaseReport",
        "ReleaseLegalHoldBody",
        "RetentionHoldCommand",
        "RetentionHoldResult",
        "RevokeDecisionBody",
        "RotationStatusResponse",
        "ServiceHealthResponse",
        "VerifiedEvidenceFile",
    }
    assert expected_models <= set(PUBLIC_MODELS)
    documents = canonical_golden_documents()
    assert {
        "controls/decision-revoke.json",
        "controls/decision-record.json",
        "controls/legal-hold-create.json",
        "controls/legal-hold-release.json",
        "controls/legal-hold-view.json",
        "operations/audit-export-request.json",
        "operations/audit-export-manifest.json",
        "operations/health-ready.json",
        "operations/rotation-status.json",
        "release/evidence-ref.json",
        "release/evidence-verification.json",
        "retention/hold-command.json",
        "retention/hold-result.json",
    } <= set(documents)


def test_public_timestamps_reject_naive_values() -> None:
    payload = canonical_golden_documents()["controls/create-case.json"]
    payload["last_known"]["time"] = "2026-01-02T03:04:05"
    with pytest.raises(ValidationError, match="timezone-aware"):
        CreateCaseBody.model_validate(payload)

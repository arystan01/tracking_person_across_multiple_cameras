from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_dashboard_has_no_simulator_storage_or_internal_calls():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "frontend" / "src").glob("*.tsx"))
        + sorted((ROOT / "frontend" / "src").glob("*.ts"))
    )
    forbidden = (
        "DemoControlAPI",
        "Simulator",
        "sqlite3",
        "/internal/",
        "localStorage.setItem",
        "reference_image_ref",
        "object_key",
        "Authorization: Bearer",
    )
    assert not [token for token in forbidden if token in source]


def test_browser_client_only_targets_same_origin_dashboard_bff():
    source = (ROOT / "frontend" / "src" / "controls.ts").read_text(
        encoding="utf-8"
    )
    assert "'/dashboard-api/controls'" in source
    assert "location.origin" in source
    assert "authorization" not in source.lower()


def test_dashboard_runtime_has_no_case_database_or_routing_implementation():
    dashboard_files = sorted((ROOT / "javes_dashboard").glob("*.py"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in dashboard_files)
    assert "sqlalchemy" not in source.lower()
    assert "sqlite" not in source.lower()
    assert "javes_routing" not in source
    assert "javes_perception" not in source

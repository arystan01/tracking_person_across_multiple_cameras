from __future__ import annotations

import pytest

from javes_main.settings import MainSettings


def test_settings_require_explicit_storage_and_hide_worker_secret(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_ARTIFACT_ROOT", raising=False)
    with pytest.raises(ValueError, match="required"):
        MainSettings.from_env("TEST_")
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///main.sqlite")
    monkeypatch.setenv("TEST_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_PERCEPTION_WORKER_TOKEN", "top-secret")
    monkeypatch.setenv("TEST_PERCEPTION_WORKER_TOKEN_PREVIOUS", "previous-secret")
    monkeypatch.setenv("TEST_JOB_LEASE_S", "45")
    monkeypatch.setenv("TEST_REQUIRE_HTTPS", "true")
    monkeypatch.setenv("TEST_TRUSTED_PROXY_CIDRS", "10.0.0.0/8,192.0.2.10/32")
    monkeypatch.setenv("TEST_CORS_ORIGINS", "https://dashboard.example")
    monkeypatch.setenv("TEST_DATABASE_STATEMENT_TIMEOUT_MS", "12345")
    settings = MainSettings.from_env("TEST_")
    assert settings.job_lease_s == 45
    assert settings.perception_worker_token.get_secret_value() == "top-secret"
    assert (
        settings.perception_worker_token_previous.get_secret_value()
        == "previous-secret"
    )
    assert settings.require_https is True
    assert settings.trusted_proxy_cidrs == ["10.0.0.0/8", "192.0.2.10/32"]
    assert settings.cors_origins == ["https://dashboard.example"]
    assert settings.database_statement_timeout_ms == 12345
    assert "top-secret" not in repr(settings)
    assert "previous-secret" not in repr(settings)


def test_staging_rejects_weak_or_placeholder_service_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///main.sqlite")
    monkeypatch.setenv("TEST_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_ENVIRONMENT", "staging")
    names = (
        "PERCEPTION_WORKER_TOKEN",
        "CLIP_WORKER_TOKEN",
        "RETENTION_WORKER_TOKEN",
        "WORKER_LEASE_SECRET",
        "ROUTING_TOKEN",
        "SITE_CONFIG_TOKEN",
    )
    for name in names:
        monkeypatch.setenv(f"TEST_{name}", "a" * 40)
    monkeypatch.setenv("TEST_ROUTING_TOKEN", "change-me-in-staging" * 2)
    with pytest.raises(ValueError, match="non-development"):
        MainSettings.from_env("TEST_")
    monkeypatch.setenv("TEST_ROUTING_TOKEN", "b" * 40)
    settings = MainSettings.from_env("TEST_")
    assert settings.environment == "staging"

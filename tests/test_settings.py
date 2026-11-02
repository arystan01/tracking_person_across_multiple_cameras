from __future__ import annotations

import pytest

from javes_main.settings import MainSettings


def test_settings_require_explicit_storage_and_hide_worker_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_ARTIFACT_ROOT", raising=False)
    with pytest.raises(ValueError, match="required"):
        MainSettings.from_env("TEST_")
    monkeypatch.setenv("TEST_DATABASE_URL", "sqlite:///main.sqlite")
    monkeypatch.setenv("TEST_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("TEST_WORKER_TOKEN", "top-secret")
    monkeypatch.setenv("TEST_JOB_LEASE_S", "45")
    settings = MainSettings.from_env("TEST_")
    assert settings.job_lease_s == 45
    assert settings.worker_token.get_secret_value() == "top-secret"
    assert "top-secret" not in repr(settings)

"""Typed process configuration loaded explicitly from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class MainSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str
    artifact_root: Path
    perception_worker_token: SecretStr | None = None
    perception_worker_token_previous: SecretStr | None = None
    clip_worker_token: SecretStr | None = None
    clip_worker_token_previous: SecretStr | None = None
    retention_worker_token: SecretStr | None = None
    retention_worker_token_previous: SecretStr | None = None
    worker_lease_secret: SecretStr | None = None
    routing_url: str | None = None
    routing_token: SecretStr | None = None
    site_config_url: str | None = None
    site_config_token: SecretStr | None = None
    routing_params_version: str = "1"
    scheduler_poll_s: float = Field(default=1.0, gt=0.0)
    job_lease_s: int = Field(default=30, ge=5)
    outbox_lease_s: int = Field(default=30, ge=5)
    session_ttl_s: int = Field(default=8 * 60 * 60, ge=300)
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout_s: int = Field(default=30, ge=1, le=300)
    database_statement_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    require_https: bool = False
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=list)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, prefix: str = "JAVES_") -> MainSettings:
        database_url = os.environ.get(f"{prefix}DATABASE_URL")
        artifact_root = os.environ.get(f"{prefix}ARTIFACT_ROOT")
        if not database_url or not artifact_root:
            raise ValueError(
                f"{prefix}DATABASE_URL and {prefix}ARTIFACT_ROOT are required"
            )
        values = {
            "database_url": database_url,
            "artifact_root": artifact_root,
            "perception_worker_token": os.environ.get(
                f"{prefix}PERCEPTION_WORKER_TOKEN"
            ),
            "perception_worker_token_previous": os.environ.get(
                f"{prefix}PERCEPTION_WORKER_TOKEN_PREVIOUS"
            ),
            "clip_worker_token": os.environ.get(f"{prefix}CLIP_WORKER_TOKEN"),
            "clip_worker_token_previous": os.environ.get(
                f"{prefix}CLIP_WORKER_TOKEN_PREVIOUS"
            ),
            "retention_worker_token": os.environ.get(
                f"{prefix}RETENTION_WORKER_TOKEN"
            ),
            "retention_worker_token_previous": os.environ.get(
                f"{prefix}RETENTION_WORKER_TOKEN_PREVIOUS"
            ),
            "worker_lease_secret": os.environ.get(f"{prefix}WORKER_LEASE_SECRET"),
            "routing_url": os.environ.get(f"{prefix}ROUTING_URL"),
            "routing_token": os.environ.get(f"{prefix}ROUTING_TOKEN"),
            "site_config_url": os.environ.get(f"{prefix}SITE_CONFIG_URL"),
            "site_config_token": os.environ.get(f"{prefix}SITE_CONFIG_TOKEN"),
            "routing_params_version": os.environ.get(
                f"{prefix}ROUTING_PARAMS_VERSION", "1"
            ),
            "require_https": os.environ.get(f"{prefix}REQUIRE_HTTPS", "false").lower()
            in {"1", "true", "yes"},
            "trusted_proxy_cidrs": [
                item.strip()
                for item in os.environ.get(f"{prefix}TRUSTED_PROXY_CIDRS", "").split(",")
                if item.strip()
            ],
            "cors_origins": [
                item.strip()
                for item in os.environ.get(f"{prefix}CORS_ORIGINS", "").split(",")
                if item.strip()
            ],
            "log_level": os.environ.get(f"{prefix}LOG_LEVEL", "INFO"),
        }
        numeric = {
            "scheduler_poll_s": float,
            "job_lease_s": int,
            "outbox_lease_s": int,
            "session_ttl_s": int,
            "database_pool_size": int,
            "database_max_overflow": int,
            "database_pool_timeout_s": int,
            "database_statement_timeout_ms": int,
        }
        for field, converter in numeric.items():
            value = os.environ.get(f"{prefix}{field.upper()}")
            if value is not None:
                values[field] = converter(value)
        return cls.model_validate(values)

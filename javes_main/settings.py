"""Typed process configuration loaded explicitly from environment variables."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class MainSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str
    artifact_root: Path
    environment: str = "development"
    perception_worker_token: SecretStr | None = None
    perception_worker_token_previous: SecretStr | None = None
    perception_worker_token_previous_valid_until: datetime | None = None
    clip_worker_token: SecretStr | None = None
    clip_worker_token_previous: SecretStr | None = None
    clip_worker_token_previous_valid_until: datetime | None = None
    retention_worker_token: SecretStr | None = None
    retention_worker_token_previous: SecretStr | None = None
    retention_worker_token_previous_valid_until: datetime | None = None
    worker_lease_secret: SecretStr | None = None
    worker_lease_secret_previous: SecretStr | None = None
    worker_lease_secret_previous_valid_until: datetime | None = None
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
            "environment": os.environ.get(f"{prefix}ENVIRONMENT", "development"),
            "perception_worker_token": os.environ.get(
                f"{prefix}PERCEPTION_WORKER_TOKEN"
            ),
            "perception_worker_token_previous": os.environ.get(
                f"{prefix}PERCEPTION_WORKER_TOKEN_PREVIOUS"
            ),
            "perception_worker_token_previous_valid_until": os.environ.get(
                f"{prefix}PERCEPTION_WORKER_TOKEN_PREVIOUS_VALID_UNTIL"
            ),
            "clip_worker_token": os.environ.get(f"{prefix}CLIP_WORKER_TOKEN"),
            "clip_worker_token_previous": os.environ.get(
                f"{prefix}CLIP_WORKER_TOKEN_PREVIOUS"
            ),
            "clip_worker_token_previous_valid_until": os.environ.get(
                f"{prefix}CLIP_WORKER_TOKEN_PREVIOUS_VALID_UNTIL"
            ),
            "retention_worker_token": os.environ.get(f"{prefix}RETENTION_WORKER_TOKEN"),
            "retention_worker_token_previous": os.environ.get(
                f"{prefix}RETENTION_WORKER_TOKEN_PREVIOUS"
            ),
            "retention_worker_token_previous_valid_until": os.environ.get(
                f"{prefix}RETENTION_WORKER_TOKEN_PREVIOUS_VALID_UNTIL"
            ),
            "worker_lease_secret": os.environ.get(f"{prefix}WORKER_LEASE_SECRET"),
            "worker_lease_secret_previous": os.environ.get(
                f"{prefix}WORKER_LEASE_SECRET_PREVIOUS"
            ),
            "worker_lease_secret_previous_valid_until": os.environ.get(
                f"{prefix}WORKER_LEASE_SECRET_PREVIOUS_VALID_UNTIL"
            ),
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
                for item in os.environ.get(f"{prefix}TRUSTED_PROXY_CIDRS", "").split(
                    ","
                )
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
        settings = cls.model_validate(values)
        from javes_security import RotationWindow

        for component in ("perception", "clip", "retention"):
            current = getattr(settings, f"{component}_worker_token")
            previous = getattr(settings, f"{component}_worker_token_previous")
            deadline = getattr(
                settings, f"{component}_worker_token_previous_valid_until"
            )
            if previous is not None:
                if current is None:
                    raise ValueError(
                        f"{component} previous token needs a current token"
                    )
                rotation = RotationWindow(
                    current=current.get_secret_value(),
                    previous=previous.get_secret_value(),
                    previous_valid_until=deadline,
                )
                rotation.require_ready()
        if settings.worker_lease_secret_previous is not None:
            if settings.worker_lease_secret is None:
                raise ValueError("previous worker lease secret needs a current secret")
            RotationWindow(
                current=settings.worker_lease_secret.get_secret_value(),
                previous=settings.worker_lease_secret_previous.get_secret_value(),
                previous_valid_until=settings.worker_lease_secret_previous_valid_until,
            ).require_ready()
        if settings.environment.lower() in {"staging", "production"}:
            forbidden = ("development", "change-me", "changeme", "example", "secret")
            required_secrets = {
                "perception worker token": settings.perception_worker_token,
                "clip worker token": settings.clip_worker_token,
                "retention worker token": settings.retention_worker_token,
                "worker lease secret": settings.worker_lease_secret,
                "routing token": settings.routing_token,
                "site config token": settings.site_config_token,
            }
            for label, value in required_secrets.items():
                raw = value.get_secret_value() if value is not None else ""
                if len(raw) < 32 or any(marker in raw.lower() for marker in forbidden):
                    raise ValueError(
                        f"{label} must be a non-development value of at least 32 characters"
                    )
        return settings

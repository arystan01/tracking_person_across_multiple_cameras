"""Typed process configuration loaded explicitly from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class MainSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str
    artifact_root: Path
    worker_token: SecretStr | None = None
    scheduler_poll_s: float = Field(default=1.0, gt=0.0)
    job_lease_s: int = Field(default=30, ge=5)
    outbox_lease_s: int = Field(default=30, ge=5)
    session_ttl_s: int = Field(default=8 * 60 * 60, ge=300)

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
            "worker_token": os.environ.get(f"{prefix}WORKER_TOKEN"),
        }
        numeric = {
            "scheduler_poll_s": float,
            "job_lease_s": int,
            "outbox_lease_s": int,
            "session_ttl_s": int,
        }
        for field, converter in numeric.items():
            value = os.environ.get(f"{prefix}{field.upper()}")
            if value is not None:
                values[field] = converter(value)
        return cls.model_validate(values)

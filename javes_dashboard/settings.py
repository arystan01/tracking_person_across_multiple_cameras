from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class DashboardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controls_url: str
    session_key: str
    previous_session_key: str | None = None
    environment: str = "development"
    static_root: Path = Path("frontend/dist")
    controls_timeout_s: float = Field(default=35.0, gt=0.0, le=300.0)
    require_https: bool = False

    @property
    def production_cookie(self) -> bool:
        return self.environment.lower() in {"staging", "production"}

    @classmethod
    def from_env(cls, prefix: str = "JAVES_DASHBOARD_") -> DashboardSettings:
        controls_url = os.environ.get(f"{prefix}CONTROLS_URL")
        session_key = os.environ.get(f"{prefix}SESSION_KEY")
        if not controls_url or not session_key:
            raise ValueError(
                f"{prefix}CONTROLS_URL and {prefix}SESSION_KEY are required"
            )
        settings = cls(
            controls_url=controls_url,
            session_key=session_key,
            previous_session_key=os.environ.get(f"{prefix}PREVIOUS_SESSION_KEY"),
            environment=os.environ.get(f"{prefix}ENVIRONMENT", "development"),
            static_root=Path(
                os.environ.get(f"{prefix}STATIC_ROOT", "frontend/dist")
            ),
            controls_timeout_s=float(
                os.environ.get(f"{prefix}CONTROLS_TIMEOUT_S", "35")
            ),
            require_https=os.environ.get(f"{prefix}REQUIRE_HTTPS", "false").lower()
            in {"1", "true", "yes"},
        )
        if settings.production_cookie and not settings.require_https:
            raise ValueError("staging/production Dashboard requires HTTPS")
        return settings

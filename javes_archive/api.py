"""Authenticated read-only API over the immutable NVIDIA archive manifest."""

from __future__ import annotations

import secrets
from collections.abc import Iterable

from fastapi import Body, FastAPI, Header, HTTPException

from javes_contracts import (
    ServiceHealthResponse,
    VideoSegmentRequest,
    VideoSegmentResult,
)

from .registry import NvidiaArchiveRegistry
from javes_telemetry import current_runtime, instrument_fastapi, operation
from javes_security import RotationConfigurationError, RotationWindow


def create_nvidia_archive_app(
    registry: NvidiaArchiveRegistry,
    *,
    service_token: str | Iterable[str] | RotationWindow,
) -> FastAPI:
    accepted_tokens = (
        service_token.accepted()
        if isinstance(service_token, RotationWindow)
        else (
            (service_token,) if isinstance(service_token, str) else tuple(service_token)
        )
    )
    if not accepted_tokens:
        raise ValueError("NVIDIA Archive service token is required")
    app = FastAPI(title="JAVES-AI NVIDIA Archive", version="1.0.0")

    def authorise(authorization: str | None) -> None:
        tokens = (
            service_token.accepted()
            if isinstance(service_token, RotationWindow)
            else accepted_tokens
        )
        bearer = (
            authorization[7:].strip()
            if authorization and authorization.lower().startswith("bearer ")
            else ""
        )
        if not any(secrets.compare_digest(bearer, token) for token in tokens):
            raise HTTPException(status_code=401, detail="valid service token required")

    @app.get(
        "/health/live",
        response_model=ServiceHealthResponse,
        response_model_exclude_none=True,
    )
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/health/ready",
        response_model=ServiceHealthResponse,
        response_model_exclude_none=True,
    )
    async def ready() -> dict[str, str | int]:
        healthy, telemetry_status = current_runtime().readiness()
        if not healthy:
            raise HTTPException(status_code=503, detail="telemetry is unavailable")
        if isinstance(service_token, RotationWindow):
            try:
                service_token.require_ready()
            except RotationConfigurationError as exc:
                raise HTTPException(
                    status_code=503, detail="token rotation is invalid"
                ) from exc
        return {
            "status": "ok",
            "telemetry": telemetry_status,
            "cameras": len(registry.manifest.cameras),
            "manifest_checksum": registry.manifest_checksum,
        }

    @app.get("/internal/v1/archive/inspect")
    async def inspect(
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorise(authorization)
        return registry.inspect()

    @app.post("/v1/video-segments", response_model=VideoSegmentResult)
    async def video_segment(
        body: dict = Body(...),
        authorization: str | None = Header(default=None),
    ) -> VideoSegmentResult:
        authorise(authorization)
        with operation(
            "archive.read", {"component": "archive", "operation": "segment"}
        ):
            return registry.segment(VideoSegmentRequest.model_validate(body))

    instrument_fastapi(app)
    return app

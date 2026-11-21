from __future__ import annotations

import secrets
from collections.abc import Iterable

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .registry import FilesystemSiteConfigRegistry
from javes_contracts import ServiceHealthResponse
from javes_telemetry import current_runtime, instrument_fastapi
from javes_security import RotationConfigurationError, RotationWindow


def create_site_config_app(
    registry: FilesystemSiteConfigRegistry,
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
        raise ValueError("Site Config service token is required")
    app = FastAPI(title="JAVES-AI Site Config", version="1.0.0")

    @app.exception_handler(KeyError)
    async def missing(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_: Request, exc: FileNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid(_: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def authorise(authorization: str | None, x_service_token: str | None) -> None:
        tokens = (
            service_token.accepted()
            if isinstance(service_token, RotationWindow)
            else accepted_tokens
        )
        bearer = (
            authorization[7:].strip()
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        if not any(
            candidate is not None
            and any(secrets.compare_digest(candidate, token) for token in tokens)
            for candidate in (bearer, x_service_token)
        ):
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
    async def ready() -> dict[str, str]:
        healthy, status = current_runtime().readiness()
        if not healthy:
            raise HTTPException(status_code=503, detail="telemetry is unavailable")
        if isinstance(service_token, RotationWindow):
            try:
                service_token.require_ready()
            except RotationConfigurationError as exc:
                raise HTTPException(
                    status_code=503, detail="token rotation is invalid"
                ) from exc
        return {"status": "ready", "telemetry": status}

    @app.get("/internal/v1/sites/{site_id}/versions/{version}")
    async def get_site(
        site_id: str,
        version: str,
        authorization: str | None = Header(default=None),
        x_service_token: str | None = Header(default=None),
    ):
        authorise(authorization, x_service_token)
        return registry.site(site_id, version)

    @app.get("/internal/v1/sites/{site_id}/versions/{version}/assets/{asset_id}")
    async def get_asset(
        site_id: str,
        version: str,
        asset_id: str,
        authorization: str | None = Header(default=None),
        x_service_token: str | None = Header(default=None),
    ) -> Response:
        authorise(authorization, x_service_token)
        data, mime_type = registry.asset(site_id, version, asset_id)
        return Response(
            data,
            media_type=mime_type,
            headers={"cache-control": "private, immutable"},
        )

    @app.get("/internal/v1/routing-params/{version}")
    async def get_params(
        version: str,
        authorization: str | None = Header(default=None),
        x_service_token: str | None = Header(default=None),
    ):
        authorise(authorization, x_service_token)
        return registry.params(version)

    instrument_fastapi(app)
    return app

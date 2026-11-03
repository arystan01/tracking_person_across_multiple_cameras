from __future__ import annotations

import secrets

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .registry import FilesystemSiteConfigRegistry


def create_site_config_app(
    registry: FilesystemSiteConfigRegistry, *, service_token: str
) -> FastAPI:
    if not service_token:
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
        bearer = (
            authorization[7:].strip()
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        if not any(
            candidate is not None
            and secrets.compare_digest(candidate, service_token)
            for candidate in (bearer, x_service_token)
        ):
            raise HTTPException(status_code=401, detail="valid service token required")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

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

    return app

from __future__ import annotations

import secrets
from collections.abc import Iterable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from javes_contracts import (
    RoutingInitRequest,
    RoutingInitResponse,
    RoutingStepRequest,
    RoutingStepResponse,
)
from javes_routing.core.params import RoutingParams
from javes_routing.core.step import init_state
from javes_routing.core.facade import step_and_render
from javes_telemetry import current_runtime, instrument_fastapi, operation
from javes_security import RotationConfigurationError, RotationWindow


def create_routing_app(
    site_config, *, service_token: str | Iterable[str] | RotationWindow
) -> FastAPI:
    """Create the network shell; all algorithmic work remains pure."""

    accepted_tokens = (
        service_token.accepted()
        if isinstance(service_token, RotationWindow)
        else (
            (service_token,) if isinstance(service_token, str) else tuple(service_token)
        )
    )
    if not accepted_tokens:
        raise ValueError("Routing service token is required")
    app = FastAPI(title="JAVES-AI Routing", version="1.0.0")

    @app.exception_handler(KeyError)
    async def missing(_: Request, exc: KeyError):
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

    def site_snapshot(site_id: str, version: str):
        method = getattr(site_config, "site_snapshot", None) or getattr(
            site_config, "site"
        )
        return method(site_id, version)

    def params_snapshot(version: str):
        method = getattr(site_config, "params_snapshot", None) or getattr(
            site_config, "params"
        )
        return method(version)

    def floor_assets(site_id: str, version: str, snapshot) -> dict[str, bytes]:
        method = getattr(site_config, "get_floor_assets", None)
        if method is not None:
            return method(site_id, version)
        return {
            item.asset_id: site_config.asset(site_id, version, item.asset_id)[0]
            for item in snapshot.assets
        }

    def anchored_case(body: RoutingInitRequest):
        return body.case.model_copy(
            update={
                "last_known_camera": (
                    body.anchors.last_known_camera or body.case.last_known_camera
                ),
                "last_known_zone": (
                    body.anchors.last_known_zone or body.case.last_known_zone
                ),
                "last_known_time": (
                    body.anchors.last_known_time or body.case.last_known_time
                ),
                "last_known_bbox_xywh": (
                    body.anchors.last_known_bbox_xywh or body.case.last_known_bbox_xywh
                ),
                "last_known_footpoint_px": (
                    body.anchors.last_known_footpoint_px
                    or body.case.last_known_footpoint_px
                ),
                "last_known_map_position": (
                    body.anchors.last_known_map_position
                    or body.case.last_known_map_position
                ),
            },
            deep=True,
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
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

    @app.post("/internal/v1/routing/init", response_model=RoutingInitResponse)
    async def initialise(
        body: RoutingInitRequest,
        authorization: str | None = Header(default=None),
        x_service_token: str | None = Header(default=None),
    ) -> RoutingInitResponse:
        authorise(authorization, x_service_token)
        site = site_snapshot(body.site_id, body.site_version)
        params_data = params_snapshot(body.params_version)
        params = RoutingParams.model_validate(params_data.params)
        return RoutingInitResponse(
            state=init_state(anchored_case(body), site.graph, params),
            site_checksum=site.checksum,
            params_checksum=params_data.checksum,
        )

    @app.post("/internal/v1/routing/step", response_model=RoutingStepResponse)
    async def advance(
        body: RoutingStepRequest,
        authorization: str | None = Header(default=None),
        x_service_token: str | None = Header(default=None),
    ) -> RoutingStepResponse:
        authorise(authorization, x_service_token)
        site = site_snapshot(body.site_id, body.site_version)
        params_data = params_snapshot(body.params_version)
        params = RoutingParams.model_validate(params_data.params)
        with operation(
            "routing.calculate", {"component": "routing", "operation": "step"}
        ):
            result = step_and_render(
                body.state,
                body.probe_results,
                site.graph,
                body.anchors,
                params,
                body.now,
                floor_assets(body.site_id, body.site_version, site),
            )
        return RoutingStepResponse(
            result=result,
            site_checksum=site.checksum,
            params_checksum=params_data.checksum,
        )

    instrument_fastapi(app)
    return app

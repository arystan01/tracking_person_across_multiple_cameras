from __future__ import annotations

import json
import mimetypes
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from javes_telemetry import instrument_fastapi, operation, record_counter

from .session import DashboardSession, SessionCodec
from .settings import DashboardSettings


MAX_BODY_BYTES = 11 * 1024 * 1024
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "authorization",
    "cookie",
    "set-cookie",
}

_ALLOWED_CONTROLS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/v1/sites$"),
        ("GET", r"^/v1/cases(?:/[^/]+)?$"),
        ("POST", r"^/v1/cases$"),
        ("POST", r"^/v1/cases/[^/]+/reference-image$"),
        ("GET", r"^/v1/cases/[^/]+/(?:view|events|clips|live|reference|export)$"),
        ("GET", r"^/v1/cases/[^/]+/clips/[^/]+$"),
        ("GET", r"^/v1/cases/[^/]+/routes/\d+/[^/]+\.svg$"),
        ("GET", r"^/v1/cases/[^/]+/candidates/[^/]+/thumbnails/\d+$"),
        ("GET", r"^/v1/cases/[^/]+/decisions$"),
        ("POST", r"^/v1/cases/[^/]+/decisions$"),
        ("POST", r"^/v1/cases/[^/]+/decisions/[^/]+/revoke$"),
        ("GET", r"^/v1/cases/[^/]+/legal-holds$"),
        ("POST", r"^/v1/cases/[^/]+/legal-holds$"),
        ("POST", r"^/v1/cases/[^/]+/legal-holds/[^/]+/release$"),
        ("POST", r"^/v1/cases/[^/]+/commands/(?:close|export|delete)$"),
        ("GET", r"^/v1/admin/users$"),
        ("POST", r"^/v1/admin/users$"),
        ("PUT", r"^/v1/admin/users/[^/]+/role$"),
        ("POST", r"^/v1/admin/users/[^/]+/(?:disable|enable|reset-password)$"),
        ("GET", r"^/v1/admin/audit$"),
        ("POST", r"^/v1/audit/exports$"),
        ("GET", r"^/v1/audit/exports/[^/]+/(?:events|manifest)$"),
    )
)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str
    new_password: str


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_json(item)
            for key, item in value.items()
            if key not in {"object_key", "uri", "access_token", "token_type"}
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _problem(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"urn:javes:problem:{code.lower().replace('_', '-')}",
            "title": "Dashboard request failed",
            "status": status,
            "detail": detail,
            "code": code,
            "retryable": status >= 500 or status in {408, 429},
        },
    )


def create_dashboard_app(
    settings: DashboardSettings,
    *,
    controls_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    codec = SessionCodec(settings.session_key, settings.previous_session_key)
    owns_client = controls_client is None
    client = controls_client or httpx.AsyncClient(
        base_url=settings.controls_url.rstrip("/"),
        timeout=settings.controls_timeout_s,
        follow_redirects=False,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="JAVES-AI Dashboard", version="1.0.0", lifespan=lifespan)
    cookie_name = "__Host-javes_session" if settings.production_cookie else "javes_session"

    def set_cookie(response: Response, session: DashboardSession) -> None:
        max_age = max(int((session.expires_at - datetime.now(session.expires_at.tzinfo)).total_seconds()), 0)
        response.set_cookie(
            cookie_name,
            codec.encode(session),
            max_age=max_age,
            httponly=True,
            secure=settings.production_cookie,
            samesite="strict",
            path="/",
        )

    def clear_cookie(response: Response) -> None:
        response.delete_cookie(
            cookie_name,
            httponly=True,
            secure=settings.production_cookie,
            samesite="strict",
            path="/",
        )

    def session_from(request: Request) -> DashboardSession:
        encoded = request.cookies.get(cookie_name)
        if not encoded:
            raise HTTPException(status_code=401, detail="Dashboard session is required")
        try:
            return codec.decode(encoded)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Dashboard session expired") from exc

    def csrf(request: Request, session: DashboardSession) -> None:
        supplied = request.headers.get("x-javes-csrf", "")
        if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
            raise HTTPException(status_code=403, detail="valid CSRF token is required")

    async def controls_request(
        method: str,
        path: str,
        *,
        session: DashboardSession | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        outbound = dict(headers or {})
        if session is not None:
            outbound["authorization"] = f"Bearer {session.access_token}"
        request = client.build_request(method, path, content=content, headers=outbound)
        return await client.send(request, stream=stream)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if (
            settings.require_https
            and not request.url.path.startswith("/health/")
            and request.url.scheme != "https"
            and request.headers.get("x-forwarded-proto") != "https"
        ):
            return _problem(400, "HTTPS is required", "HTTPS_REQUIRED")
        response = await call_next(request)
        response.headers["content-security-policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'"
        )
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["x-frame-options"] = "DENY"
        if settings.production_cookie:
            response.headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        response = await controls_request("GET", "/health/ready")
        if response.status_code >= 500:
            raise HTTPException(status_code=503, detail="Controls is unavailable")
        return {"status": "ready"}

    @app.post("/dashboard-api/session/login")
    async def login(body: LoginBody) -> Response:
        with operation("dashboard.session", {"component": "dashboard", "operation": "login"}):
            upstream = await controls_request(
                "POST",
                "/v1/auth/login",
                content=body.model_dump_json().encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        if upstream.status_code != 200:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type="application/problem+json",
            )
        payload = upstream.json()
        session = DashboardSession(
            session_id=str(uuid.uuid4()),
            access_token=str(payload["access_token"]),
            user_id=str(payload["user"]["user_id"]),
            csrf_token=codec.new_csrf_token(),
            expires_at=payload["expires_at"],
        )
        response = JSONResponse(
            {
                "user": _safe_json(payload["user"]),
                "expires_at": session.expires_at.isoformat(),
                "csrf_token": session.csrf_token,
            }
        )
        set_cookie(response, session)
        record_counter(
            "javes.dashboard.sessions",
            attributes={"component": "dashboard", "operation": "login", "outcome": "accepted"},
        )
        return response

    @app.get("/dashboard-api/session/me")
    async def me(request: Request) -> Response:
        session = session_from(request)
        upstream = await controls_request("GET", "/v1/auth/me", session=session)
        if upstream.status_code == 401:
            response = _problem(401, "Dashboard session expired", "AUTHENTICATION_REQUIRED")
            clear_cookie(response)
            return response
        if upstream.status_code != 200:
            return Response(upstream.content, upstream.status_code, media_type="application/problem+json")
        return JSONResponse(
            {
                "user": _safe_json(upstream.json()),
                "expires_at": session.expires_at.isoformat(),
                "csrf_token": session.csrf_token,
            }
        )

    @app.post("/dashboard-api/session/logout", status_code=204)
    async def logout(request: Request) -> Response:
        session = session_from(request)
        csrf(request, session)
        upstream = await controls_request("POST", "/v1/auth/logout", session=session)
        response = Response(status_code=204 if upstream.status_code in {204, 401} else upstream.status_code)
        clear_cookie(response)
        return response

    @app.post("/dashboard-api/session/change-password", status_code=204)
    async def change_password(body: ChangePasswordBody, request: Request) -> Response:
        session = session_from(request)
        csrf(request, session)
        key = request.headers.get("idempotency-key") or str(uuid.uuid4())
        upstream = await controls_request(
            "POST",
            "/v1/auth/change-password",
            session=session,
            content=body.model_dump_json().encode("utf-8"),
            headers={"content-type": "application/json", "idempotency-key": key},
        )
        response = Response(upstream.content, upstream.status_code, media_type=upstream.headers.get("content-type"))
        if upstream.status_code == 204:
            clear_cookie(response)
        return response

    @app.api_route(
        "/dashboard-api/controls/v1/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
    )
    async def proxy(rest: str, request: Request) -> Response:
        path = "/v1/" + rest
        if path.startswith("/internal/") or not any(
            request.method == method and pattern.fullmatch(path)
            for method, pattern in _ALLOWED_CONTROLS
        ):
            return _problem(404, "Dashboard endpoint is not available", "NOT_FOUND")
        session = session_from(request)
        if request.method != "GET":
            csrf(request, session)
        raw_length = request.headers.get("content-length")
        if raw_length and int(raw_length) > MAX_BODY_BYTES:
            return _problem(413, "Dashboard request is too large", "PAYLOAD_TOO_LARGE")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _problem(413, "Dashboard request is too large", "PAYLOAD_TOO_LARGE")
        content_type = request.headers.get("content-type", "")
        if body and content_type.startswith("application/json"):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return _problem(422, "Request body is not valid JSON", "VALIDATION_FAILED")
            if isinstance(payload, dict):
                payload.pop("object_key", None)
                payload.pop("reference_image_ref", None)
                if path == "/v1/cases" or any(
                    marker in path
                    for marker in ("/decisions", "/commands/", "/legal-holds")
                ):
                    payload["operator_id"] = session.user_id
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"content-type", "accept", "idempotency-key", "x-request-id", "traceparent"}
        }
        media_expected = request.method == "GET" and any(
            marker in path
            for marker in ("/routes/", "/thumbnails/", "/clips/", "/live", "/reference", "/export")
        )
        operation_name = (
            "upload" if path.endswith("/reference-image") else "media" if media_expected else "proxy"
        )
        with operation(
            "dashboard.proxy",
            {"component": "dashboard", "operation": operation_name},
        ):
            upstream = await controls_request(
                request.method,
                path,
                session=session,
                content=body or None,
                headers=headers,
                stream=media_expected,
            )
        if upstream.status_code == 401:
            await upstream.aclose()
            response = _problem(401, "Dashboard session expired", "AUTHENTICATION_REQUIRED")
            clear_cookie(response)
            return response
        if upstream.status_code >= 400:
            content = await upstream.aread()
            await upstream.aclose()
            try:
                payload = json.loads(content) if content else {}
            except json.JSONDecodeError:
                payload = {}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None
            if upstream.status_code >= 500:
                detail = "Controls is temporarily unavailable"
            if not isinstance(detail, str):
                detail = "The request could not be completed"
            if not isinstance(code, str):
                code = "UPSTREAM_ERROR"
            record_counter(
                "javes.dashboard.proxy.failures",
                attributes={
                    "component": "dashboard",
                    "operation": operation_name,
                    "outcome": str(upstream.status_code),
                },
            )
            return _problem(upstream.status_code, detail, code)
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in HOP_BY_HOP
            and key.lower() in {"content-type", "content-length", "content-disposition", "retry-after"}
        }
        response_headers["cache-control"] = "private, no-store"
        if media_expected:
            async def chunks() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
            return StreamingResponse(
                chunks(),
                status_code=upstream.status_code,
                headers=response_headers,
            )
        content = await upstream.aread()
        await upstream.aclose()
        if "application/json" in upstream.headers.get("content-type", "") and content:
            try:
                payload = json.loads(content)
                if path.endswith("/reference-image") and isinstance(payload, dict):
                    payload.pop("reference_image_ref", None)
                return JSONResponse(
                    _safe_json(payload),
                    status_code=upstream.status_code,
                    headers={k: v for k, v in response_headers.items() if k.lower() != "content-length"},
                )
            except json.JSONDecodeError:
                return _problem(502, "Controls returned invalid JSON", "UPSTREAM_INVALID")
        return Response(content, upstream.status_code, headers=response_headers)

    static_root = settings.static_root.resolve()

    @app.get("/{asset_path:path}")
    async def static(asset_path: str) -> Response:
        if asset_path.startswith(("dashboard-api/", "internal/")):
            raise HTTPException(status_code=404)
        candidate = (static_root / asset_path).resolve()
        if asset_path and candidate.is_relative_to(static_root) and candidate.is_file():
            return FileResponse(candidate, media_type=mimetypes.guess_type(candidate.name)[0])
        index = static_root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Dashboard assets are not built")
        return FileResponse(index, media_type="text/html")

    instrument_fastapi(app)
    return app

"""HTTP production boundary middleware."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

from javes_main.observability import (
    bind_request_context,
    reset_request_context,
    validate_correlation_id,
    validate_traceparent,
)


def _trusted(remote: str | None, trusted_proxies: tuple[str, ...]) -> bool:
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote)
        return any(address in ipaddress.ip_network(item) for item in trusted_proxies)
    except ValueError:
        return False


def install_boundary_middleware(
    app,
    *,
    require_https: bool,
    trusted_proxies: Iterable[str] = (),
) -> None:
    trusted = tuple(trusted_proxies)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        correlation_id = validate_correlation_id(request.headers.get("x-request-id"))
        traceparent = validate_traceparent(request.headers.get("traceparent"))
        tokens = bind_request_context(correlation_id, traceparent)
        try:
            remote = request.client.host if request.client else None
            forwarded_proto = request.headers.get("x-forwarded-proto")
            secure = request.url.scheme == "https" or (
                _trusted(remote, trusted) and forwarded_proto == "https"
            )
            if require_https and not secure and request.url.path != "/health/live":
                response = JSONResponse(
                    status_code=426,
                    media_type="application/problem+json",
                    content={
                        "type": "urn:javes:problem:https-required",
                        "title": "HTTPS Required",
                        "status": 426,
                        "detail": "TLS must terminate at the service or a trusted proxy",
                        "code": "HTTPS_REQUIRED",
                        "retryable": False,
                    },
                )
            else:
                response = await call_next(request)
            response.headers["x-request-id"] = correlation_id
            response.headers["traceparent"] = traceparent
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["content-security-policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
            response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
            return response
        finally:
            reset_request_context(tokens)

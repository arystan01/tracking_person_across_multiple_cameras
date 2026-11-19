"""Dependency-free W3C trace/correlation context shared by HTTP adapters."""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar


_correlation_id: ContextVar[str | None] = ContextVar("javes_correlation_id", default=None)
_traceparent: ContextVar[str | None] = ContextVar("javes_traceparent", default=None)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")


def new_correlation_id() -> str:
    return secrets.token_hex(16)


def validate_correlation_id(value: str | None) -> str:
    return value if value and _SAFE_ID.fullmatch(value) else new_correlation_id()


def validate_traceparent(value: str | None) -> str:
    if value and _TRACEPARENT.fullmatch(value):
        return value
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def bind_request_context(correlation_id: str, traceparent: str):
    return _correlation_id.set(correlation_id), _traceparent.set(traceparent)


def reset_request_context(tokens) -> None:
    correlation_token, trace_token = tokens
    _correlation_id.reset(correlation_token)
    _traceparent.reset(trace_token)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def current_traceparent() -> str | None:
    return _traceparent.get()


def outbound_trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if value := current_correlation_id():
        headers["x-request-id"] = value
    if value := current_traceparent():
        headers["traceparent"] = value
    return headers

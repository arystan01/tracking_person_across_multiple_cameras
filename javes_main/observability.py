"""Correlation, structured logging, and secret-safe diagnostic helpers."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

from javes_contracts.trace import (
    bind_request_context as bind_request_context,
    current_correlation_id,
    current_traceparent,
    outbound_trace_headers as outbound_trace_headers,
    reset_request_context as reset_request_context,
    validate_correlation_id as validate_correlation_id,
    validate_traceparent as validate_traceparent,
)

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|credential|api[_-]?key|lease_token)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|secret|credential|api[_-]?key)=([^\s&]+)"
)
_URL_USERINFO = re.compile(r"(https?://[^:/\s]+:)([^@/\s]+)(@)", re.IGNORECASE)


def redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
        return _URL_USERINFO.sub(r"\1[REDACTED]\3", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            from javes_telemetry import current_span_ids

            trace_id, span_id = current_span_ids()
        except ImportError:
            trace_id = span_id = None
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "correlation_id": current_correlation_id(),
            "traceparent": current_traceparent(),
            "trace_id": trace_id,
            "span_id": span_id,
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    try:
        from javes_telemetry import logging_handler

        otel_handler = logging_handler()
        if otel_handler is not None:
            root.handlers.append(otel_handler)
    except ImportError:
        pass
    root.setLevel(level.upper())

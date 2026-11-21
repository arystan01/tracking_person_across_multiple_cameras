"""OpenTelemetry service-shell integration for JAVES-AI.

Pure algorithm packages must never import this package.  Runtime shells use the
small public API below so telemetry remains optional and does not leak domain
payloads into observability signals.
"""

from .runtime import (
    TelemetryConfigurationError,
    TelemetryRuntime,
    TelemetrySettings,
    configure_telemetry,
    current_runtime,
    instrument_fastapi,
    current_span_ids,
    logging_handler,
    operation,
    record_counter,
    record_histogram,
    safe_attributes,
    shutdown_telemetry,
)

__all__ = [
    "TelemetryConfigurationError",
    "TelemetryRuntime",
    "TelemetrySettings",
    "configure_telemetry",
    "current_runtime",
    "instrument_fastapi",
    "current_span_ids",
    "logging_handler",
    "operation",
    "record_counter",
    "record_histogram",
    "safe_attributes",
    "shutdown_telemetry",
]

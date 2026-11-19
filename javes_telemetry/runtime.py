from __future__ import annotations

import atexit
import copy
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "component",
        "operation",
        "task.state",
        "task.component",
        "error.code",
        "outcome",
        "retryable",
        "duplicate",
        "http.route",
        "routing.result",
        "retention.action",
    }
)
_SENSITIVE = re.compile(
    r"token|secret|password|credential|authorization|cookie|embedding|bbox|image|path|case_id|candidate|tracklet|person",
    re.IGNORECASE,
)
_RESOURCE_KEYS = frozenset(
    {
        "service.namespace",
        "service.version",
        "service.instance.id",
        "deployment.environment.name",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|token=|secret=|password=|credential=)([^\s&,;]+)"
)


class TelemetryConfigurationError(ValueError):
    """Raised before service readiness when enabled telemetry is invalid."""


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise TelemetryConfigurationError(f"invalid boolean value {value!r}")


def _resource_attributes(raw: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not raw:
        return result
    for entry in raw.split(","):
        key, separator, value = entry.partition("=")
        key = key.strip()
        if not separator or key not in _RESOURCE_KEYS:
            raise TelemetryConfigurationError(
                f"OTEL_RESOURCE_ATTRIBUTES contains unsupported key {key!r}"
            )
        if _SENSITIVE.search(value):
            raise TelemetryConfigurationError(
                f"resource attribute {key!r} contains a sensitive-looking value"
            )
        result[key] = value.strip()
    return result


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool = False
    endpoint: str | None = None
    service_name: str = "javes-service"
    service_version: str = "0.1.0"
    environment: str = "local"
    instance_id: str | None = None
    sample_ratio: float = 1.0
    export_timeout_s: float = 5.0
    insecure: bool = False
    resource_attributes: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, service_name: str, *, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        enabled = _bool(env.get("JAVES_OTEL_ENABLED"), False)
        endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        ratio_raw = env.get("OTEL_TRACES_SAMPLER_ARG", "1.0")
        timeout_raw = env.get("JAVES_OTEL_EXPORT_TIMEOUT_S", "5")
        try:
            ratio = float(ratio_raw)
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise TelemetryConfigurationError(
                "OTel sample ratio and timeout must be numeric"
            ) from exc
        settings = cls(
            enabled=enabled,
            endpoint=endpoint,
            service_name=service_name,
            service_version=env.get("JAVES_SERVICE_VERSION", "0.1.0"),
            environment=env.get("JAVES_DEPLOYMENT_ENVIRONMENT", "local"),
            instance_id=env.get("JAVES_INSTANCE_ID"),
            sample_ratio=ratio,
            export_timeout_s=timeout,
            insecure=_bool(env.get("OTEL_EXPORTER_OTLP_INSECURE"), False),
            resource_attributes=_resource_attributes(
                env.get("OTEL_RESOURCE_ATTRIBUTES")
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.endpoint:
            raise TelemetryConfigurationError(
                "JAVES_OTEL_ENABLED requires OTEL_EXPORTER_OTLP_ENDPOINT"
            )
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise TelemetryConfigurationError("OTLP endpoint must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TelemetryConfigurationError(
                "OTLP endpoint must not contain credentials, query or fragment"
            )
        if self.insecure and parsed.scheme != "http":
            raise TelemetryConfigurationError(
                "OTLP insecure mode requires an http endpoint"
            )
        if not 0.0 <= self.sample_ratio <= 1.0:
            raise TelemetryConfigurationError("trace sample ratio must be in [0, 1]")
        if self.export_timeout_s <= 0:
            raise TelemetryConfigurationError("OTLP export timeout must be positive")
        if not self.service_name.strip():
            raise TelemetryConfigurationError("service name is required")


def safe_attributes(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only bounded, non-person, non-secret low-cardinality attributes."""

    result: dict[str, Any] = {}
    for key, value in (values or {}).items():
        key = str(key)
        if key not in _SAFE_ATTRIBUTE_KEYS or _SENSITIVE.search(key):
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif (
            isinstance(value, str) and len(value) <= 80 and not _SENSITIVE.search(value)
        ):
            result[key] = value
    return result


def _redact_text(value: str) -> str:
    return _SECRET_TEXT.sub(lambda match: match.group(1) + "[REDACTED]", value)


def _signal_endpoint(base: str, signal: str) -> str:
    parsed = urlsplit(base.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith(f"/v1/{signal}"):
        path += f"/v1/{signal}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class TelemetryRuntime:
    def __init__(
        self,
        settings: TelemetrySettings,
        *,
        span_exporter=None,
        metric_reader=None,
        log_exporter=None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.enabled = settings.enabled
        self.error: str | None = None
        self._instrumented_apps: set[int] = set()
        self._shutdown = False
        self.tracer_provider = self.meter_provider = self.logger_provider = None
        if not settings.enabled:
            return
        try:
            self._initialise(span_exporter, metric_reader, log_exporter)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise TelemetryConfigurationError(
                f"OpenTelemetry initialisation failed: {self.error}"
            ) from exc

    def _initialise(self, span_exporter, metric_reader, log_exporter) -> None:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        attributes = {
            "service.name": self.settings.service_name,
            "service.version": self.settings.service_version,
            "deployment.environment.name": self.settings.environment,
            **dict(self.settings.resource_attributes),
        }
        if self.settings.instance_id:
            attributes["service.instance.id"] = self.settings.instance_id
        resource = Resource.create(attributes)
        timeout_ms = int(self.settings.export_timeout_s * 1000)
        if span_exporter is None:
            span_exporter = OTLPSpanExporter(
                endpoint=_signal_endpoint(self.settings.endpoint or "", "traces"),
                timeout=self.settings.export_timeout_s,
            )
            span_processor = BatchSpanProcessor(
                span_exporter, export_timeout_millis=timeout_ms
            )
        else:
            span_processor = SimpleSpanProcessor(span_exporter)
        self.tracer_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(self.settings.sample_ratio)),
        )
        self.tracer_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(self.tracer_provider)

        if metric_reader is None:
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=_signal_endpoint(self.settings.endpoint or "", "metrics"),
                    timeout=self.settings.export_timeout_s,
                ),
                export_interval_millis=10000,
                export_timeout_millis=timeout_ms,
            )
        self.meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(self.meter_provider)

        self.logger_provider = LoggerProvider(resource=resource)
        if log_exporter is None:
            log_exporter = OTLPLogExporter(
                endpoint=_signal_endpoint(self.settings.endpoint or "", "logs"),
                timeout=self.settings.export_timeout_s,
            )
            log_processor = BatchLogRecordProcessor(log_exporter)
        else:
            log_processor = SimpleLogRecordProcessor(log_exporter)
        self.logger_provider.add_log_record_processor(log_processor)
        set_logger_provider(self.logger_provider)

    def instrument_fastapi(self, app) -> None:
        if not self.enabled or id(app) in self._instrumented_apps:
            return
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            excluded_urls="/health/live",
        )
        self._instrumented_apps.add(id(app))

    def readiness(self) -> tuple[bool, str]:
        if not self.enabled:
            return True, "disabled"
        return (self.error is None, "ready" if self.error is None else self.error)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for provider in (
            self.logger_provider,
            self.meter_provider,
            self.tracer_provider,
        ):
            if provider is not None:
                provider.force_flush()
                provider.shutdown()


_LOCK = threading.RLock()
_RUNTIME = TelemetryRuntime(TelemetrySettings())
_HTTPX_INSTRUMENTED = False
_INSTRUMENTS: dict[tuple[str, str], Any] = {}


def configure_telemetry(
    service_name: str,
    *,
    settings: TelemetrySettings | None = None,
    span_exporter=None,
    metric_reader=None,
    log_exporter=None,
) -> TelemetryRuntime:
    global _RUNTIME, _HTTPX_INSTRUMENTED
    with _LOCK:
        wanted = settings or TelemetrySettings.from_env(service_name)
        if _RUNTIME.enabled:
            if _RUNTIME.settings == wanted:
                return _RUNTIME
            raise TelemetryConfigurationError(
                "OpenTelemetry is already initialised with different settings"
            )
        _RUNTIME = TelemetryRuntime(
            wanted,
            span_exporter=span_exporter,
            metric_reader=metric_reader,
            log_exporter=log_exporter,
        )
        if _RUNTIME.enabled and not _HTTPX_INSTRUMENTED:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument(
                tracer_provider=_RUNTIME.tracer_provider,
                meter_provider=_RUNTIME.meter_provider,
            )
            _HTTPX_INSTRUMENTED = True
        atexit.register(_RUNTIME.shutdown)
        return _RUNTIME


def current_runtime() -> TelemetryRuntime:
    return _RUNTIME


def instrument_fastapi(app) -> None:
    _RUNTIME.instrument_fastapi(app)


def shutdown_telemetry() -> None:
    _RUNTIME.shutdown()


def current_span_ids() -> tuple[str | None, str | None]:
    if not _RUNTIME.enabled:
        return None, None
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


class _SafeOtelLoggingHandler(logging.Handler):
    def __init__(self, delegate: logging.Handler) -> None:
        super().__init__()
        self.delegate = delegate

    def emit(self, record: logging.LogRecord) -> None:
        safe = copy.copy(record)
        safe.msg = _redact_text(record.getMessage())
        safe.args = ()
        safe.exc_info = None
        safe.exc_text = None
        self.delegate.emit(safe)

    def flush(self) -> None:
        self.delegate.flush()


def logging_handler(level: int = logging.NOTSET) -> logging.Handler | None:
    if not _RUNTIME.enabled or _RUNTIME.logger_provider is None:
        return None
    from opentelemetry.sdk._logs import LoggingHandler

    return _SafeOtelLoggingHandler(
        LoggingHandler(level=level, logger_provider=_RUNTIME.logger_provider)
    )


@contextmanager
def operation(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    if not _RUNTIME.enabled:
        yield None
        return
    from opentelemetry import trace

    with trace.get_tracer("javes.runtime").start_as_current_span(
        name, attributes=safe_attributes(attributes)
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error.code", type(exc).__name__[:80])
            span.record_exception(exc, attributes={"exception.message": "redacted"})
            raise


def record_counter(
    name: str, value: int = 1, attributes: Mapping[str, Any] | None = None
) -> None:
    if not _RUNTIME.enabled:
        return
    from opentelemetry import metrics

    instrument = _INSTRUMENTS.get(("counter", name))
    if instrument is None:
        instrument = metrics.get_meter("javes.runtime").create_counter(name)
        _INSTRUMENTS[("counter", name)] = instrument
    instrument.add(value, safe_attributes(attributes))


def record_histogram(
    name: str, value: float, attributes: Mapping[str, Any] | None = None
) -> None:
    if not _RUNTIME.enabled:
        return
    from opentelemetry import metrics

    instrument = _INSTRUMENTS.get(("histogram", name))
    if instrument is None:
        instrument = metrics.get_meter("javes.runtime").create_histogram(name)
        _INSTRUMENTS[("histogram", name)] = instrument
    instrument.record(value, safe_attributes(attributes))

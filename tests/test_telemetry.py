from __future__ import annotations

import logging
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import propagate, trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from javes_telemetry import (
    TelemetryConfigurationError,
    TelemetrySettings,
    configure_telemetry,
    current_span_ids,
    instrument_fastapi,
    logging_handler,
    operation,
    record_counter,
    safe_attributes,
)


SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader()
LOG_EXPORTER = InMemoryLogRecordExporter()


@pytest.fixture(scope="module", autouse=True)
def telemetry_runtime():
    runtime = configure_telemetry(
        "javes-telemetry-test",
        settings=TelemetrySettings(
            enabled=True,
            endpoint="http://collector:4318",
            service_name="javes-telemetry-test",
            environment="test",
            insecure=True,
        ),
        span_exporter=SPAN_EXPORTER,
        metric_reader=METRIC_READER,
        log_exporter=LOG_EXPORTER,
    )
    yield runtime


def test_enabled_configuration_fails_fast_without_safe_endpoint() -> None:
    with pytest.raises(TelemetryConfigurationError):
        TelemetrySettings.from_env(
            "broken",
            environ={"JAVES_OTEL_ENABLED": "true"},
        )
    with pytest.raises(TelemetryConfigurationError):
        TelemetrySettings.from_env(
            "broken",
            environ={
                "JAVES_OTEL_ENABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:secret@example.test",
            },
        )


def test_safe_attributes_drop_secrets_person_data_and_cardinality() -> None:
    assert safe_attributes(
        {
            "component": "retention",
            "operation": "delete",
            "authorization": "Bearer secret",
            "case_id": "case-123",
            "bbox": [1, 2, 3, 4],
            "unknown": "value",
        }
    ) == {"component": "retention", "operation": "delete"}


def test_parent_child_w3c_propagation_and_no_sensitive_span_attributes() -> None:
    SPAN_EXPORTER.clear()
    app = FastAPI()

    @app.get("/internal/v1/workers/{component}/tasks/claim")
    async def claim(component: str):
        with operation(
            "worker.claim",
            {
                "component": component,
                "operation": "claim",
                "case_id": "must-not-export",
                "token": "must-not-export",
            },
        ):
            return {"tasks": []}

    instrument_fastapi(app)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("worker-client") as parent:
        headers: dict[str, str] = {}
        propagate.inject(headers)

        async def request():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://main.test",
            ) as client:
                return await client.get(
                    "/internal/v1/workers/perception/tasks/claim",
                    headers=headers,
                )

        response = asyncio.run(request())
        parent_span_id = parent.get_span_context().span_id
    assert response.status_code == 200
    spans = SPAN_EXPORTER.get_finished_spans()
    server = next(
        item
        for item in spans
        if item.name == "GET /internal/v1/workers/{component}/tasks/claim"
    )
    assert server.parent is not None
    assert server.parent.span_id == parent_span_id
    exported = " ".join(
        f"{key}={value}" for item in spans for key, value in item.attributes.items()
    )
    assert "must-not-export" not in exported
    assert "Bearer" not in exported


def test_metrics_and_correlated_logs_are_exported_redacted() -> None:
    LOG_EXPORTER.clear()
    handler = logging_handler()
    assert handler is not None
    logger = logging.getLogger("javes.telemetry.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        with operation("retention.delete", {"component": "retention"}):
            trace_id, span_id = current_span_ids()
            logger.info("token=super-secret operation completed")
            record_counter(
                "javes.retention.operations",
                attributes={"component": "retention", "outcome": "completed"},
            )
    finally:
        handler.flush()
        logger.removeHandler(handler)
    records = LOG_EXPORTER.get_finished_logs()
    assert records
    assert all("super-secret" not in str(item.log_record.body) for item in records)
    assert any("[REDACTED]" in str(item.log_record.body) for item in records)
    assert trace_id is not None and span_id is not None
    assert any(f"{item.log_record.trace_id:032x}" == trace_id for item in records)
    assert any(f"{item.log_record.span_id:016x}" == span_id for item in records)
    metrics = METRIC_READER.get_metrics_data()
    names = {
        metric.name
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "javes.retention.operations" in names

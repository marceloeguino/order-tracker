import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import main
from app.telemetry import create_telemetry


@pytest.fixture
def telemetry_sink(monkeypatch):
    """Swap the app's telemetry for one that records in memory only."""
    reader = InMemoryMetricReader()
    spans = InMemorySpanExporter()
    test_telemetry = create_telemetry(
        console=False,
        otlp_endpoint=None,
        extra_metric_readers=[reader],
        extra_span_processors=[SimpleSpanProcessor(spans)],
    )
    monkeypatch.setattr(main, "telemetry", test_telemetry)
    return reader, spans

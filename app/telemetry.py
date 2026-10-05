"""OpenTelemetry setup for the order tracker: traces, metrics and logs.

Every signal is always written to the console (so `docker compose logs app`
shows it). When OTEL_EXPORTER_OTLP_ENDPOINT is set, the same signals are also
shipped over OTLP/HTTP to the OpenTelemetry Collector.

The providers are owned by a Telemetry object instead of being registered as
process-wide globals, which keeps the app importable/testable without
"provider already set" warnings.
"""

import logging
import os
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

SERVICE_NAME = "order-tracker"
LOGGER_NAME = "order_tracker"

# Latency buckets (seconds) for the request-duration histogram.
DURATION_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5]


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Telemetry:
    tracer: trace.Tracer
    logger: logging.Logger
    request_counter: object
    request_duration: object
    _providers: list = field(default_factory=list)

    def record_request(self, method: str, route: str, status_code: int, seconds: float):
        attributes = {
            "http.request.method": method,
            "http.route": route,
            "http.response.status_code": status_code,
        }
        self.request_counter.add(1, attributes)
        self.request_duration.record(seconds, attributes)

    def seed_error_series(self, routes: list[tuple[str, str]]):
        """Create the 5xx series at 0 so Prometheus sees them *before* the first error.

        Prometheus' increase()/rate() ignore a counter's first sample, so without
        a pre-existing 0 the very first 500 on a route would never alert.
        """
        for method, route in routes:
            self.request_counter.add(
                0,
                {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_code": 500,
                },
            )

    def shutdown(self):
        for provider in self._providers:
            try:
                provider.shutdown()
            except Exception:  # pragma: no cover - best effort on exit
                pass


def create_telemetry(
    *,
    console: bool | None = None,
    otlp_endpoint: str | None = None,
    extra_metric_readers=(),
    extra_span_processors=(),
    export_interval_ms: int | None = None,
) -> Telemetry:
    """Build tracer/meter/logger providers from arguments or environment."""
    if console is None:
        console = _truthy(os.getenv("OTEL_CONSOLE_EXPORT"), True)
    if otlp_endpoint is None:
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None
    if export_interval_ms is None:
        export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000"))

    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME),
            "service.version": os.getenv("SERVICE_VERSION", "0.1.0"),
            "deployment.environment.name": os.getenv("DEPLOYMENT_ENV", "local"),
        }
    )

    # --- traces ---
    tracer_provider = TracerProvider(resource=resource)
    if console:
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    if otlp_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"))
        )
    for processor in extra_span_processors:
        tracer_provider.add_span_processor(processor)

    # --- metrics ---
    readers = list(extra_metric_readers)
    if console:
        readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(), export_interval_millis=export_interval_ms
            )
        )
    if otlp_endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics"),
                export_interval_millis=export_interval_ms,
            )
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    meter = meter_provider.get_meter(SERVICE_NAME)
    request_counter = meter.create_counter(
        "http.server.requests",
        unit="{request}",
        description="HTTP requests handled, by route and response status code",
    )
    request_duration = meter.create_histogram(
        "http.server.request.duration",
        unit="s",
        description="HTTP server request duration",
        explicit_bucket_boundaries_advisory=DURATION_BUCKETS,
    )

    # --- logs ---
    logger_provider = LoggerProvider(resource=resource)
    if console:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))
    if otlp_endpoint:
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/logs"))
        )
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # Re-creating Telemetry (tests) must not stack handlers.
    for handler in [h for h in logger.handlers if isinstance(h, LoggingHandler)]:
        logger.removeHandler(handler)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    logger.propagate = False

    return Telemetry(
        tracer=tracer_provider.get_tracer(SERVICE_NAME),
        logger=logger,
        request_counter=request_counter,
        request_duration=request_duration,
        _providers=[tracer_provider, meter_provider, logger_provider],
    )

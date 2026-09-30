"""OpenTelemetry setup: exports traces, metrics, and logs to the console.

FastAPI's built-in telemetry (FastAPI(telemetry=...)) auto-instruments every
request with a span and an `http.server.request.duration` histogram tagged
with `http.route` and `http.response.status_code`. It picks up whichever
tracer/meter/logger providers are registered globally, so we just need to
register console-exporting providers before the app starts serving traffic.
"""
import logging

from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

SERVICE_NAME = "order-tracker"

# Proxy that forwards to the real global tracer provider once configure_telemetry() runs.
tracer = trace.get_tracer(SERVICE_NAME)


def configure_telemetry():
    """Register global tracer/meter/logger providers with console exporters."""
    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=15000)],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter()))
    _logs.set_logger_provider(logger_provider)
    logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
    logging.getLogger().setLevel(logging.INFO)

    return tracer_provider, meter_provider, logger_provider


def shutdown_telemetry(tracer_provider, meter_provider, logger_provider):
    """Flush and close all providers so buffered signals are exported before exit."""
    tracer_provider.shutdown()
    meter_provider.shutdown()
    logger_provider.shutdown()

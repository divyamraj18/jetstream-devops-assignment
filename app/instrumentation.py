import os

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "jetstream-articles-api")
OTEL_EXPORTER_ENDPOINT = os.getenv("OTEL_EXPORTER_ENDPOINT", "localhost:4317")


def setup_telemetry(app: FastAPI) -> None:
    resource = Resource.create({"service.name": SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTEL_EXPORTER_ENDPOINT, insecure=True)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    PymongoInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app)

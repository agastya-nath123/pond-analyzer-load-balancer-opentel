"""
OpenTelemetry setup: traces and metrics, sent wherever the standard OTEL_*
environment variables say.

Telemetry is off unless you ask for it, so the service behaves exactly as
before when no collector is running:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
        send traces and metrics to an OTLP collector (OTLP over HTTP), such
        as Jaeger, Grafana's otel-lgtm image or the OpenTelemetry Collector
    OTEL_TRACES_EXPORTER=console / OTEL_METRICS_EXPORTER=console
        print them in this terminal instead (values: otlp, console, none;
        several can be combined, e.g. "otlp,console")
    OTEL_SERVICE_NAME           override the service name
    OTEL_METRIC_EXPORT_INTERVAL milliseconds between metric exports (10000 here)
    OTEL_SDK_DISABLED=true      turn telemetry off even if the above are set

Other standard variables (OTEL_EXPORTER_OTLP_HEADERS,
OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER, ...) work as usual.

Code that records spans or metrics only needs the opentelemetry-api
package: without setup() its tracers and meters do nothing.
"""

from __future__ import annotations

import os
import socket

ENABLED = False


def _exporters(kind: str) -> set[str]:
    """Exporters requested for 'TRACES' or 'METRICS'."""
    value = os.environ.get(f"OTEL_{kind}_EXPORTER")
    if value is None:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(f"OTEL_EXPORTER_OTLP_{kind}_ENDPOINT")
        value = "otlp" if endpoint else "none"
    names = {v.strip().lower() for v in value.split(",") if v.strip()} - {"none"}
    unknown = names - {"otlp", "console"}
    if unknown:
        raise ValueError(f"OTEL_{kind}_EXPORTER: unknown exporter {', '.join(sorted(unknown))}; use otlp, console or none.")
    return names


def setup(service_name: str, service_version: str) -> bool:
    """Start exporting traces and metrics if configured. Returns True if it did."""
    global ENABLED
    if ENABLED:
        return True
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    trace_exporters, metric_exporters = _exporters("TRACES"), _exporters("METRICS")
    if not trace_exporters and not metric_exporters:
        return False

    from opentelemetry import metrics, trace
    from opentelemetry.sdk.resources import Resource

    # service.instance.id tells the processes apart when uvicorn runs
    # several workers, or two copies of the service run side by side.
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
        "service.version": service_version,
        "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
    })

    if trace_exporters:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        provider = TracerProvider(resource=resource)
        for name in sorted(trace_exporters):
            if name == "otlp":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter()
            else:
                exporter = ConsoleSpanExporter()
            # Spans are sent in batches from a background thread, so a slow or
            # missing collector never delays a request.
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

    if metric_exporters:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader

        interval = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
        readers = []
        for name in sorted(metric_exporters):
            if name == "otlp":
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                exporter = OTLPMetricExporter()
            else:
                exporter = ConsoleMetricExporter()
            readers.append(PeriodicExportingMetricReader(exporter, export_interval_millis=interval))
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    ENABLED = True
    return True


class FreshTraceContext:
    """
    ASGI wrapper that starts every HTTP request with an empty OpenTelemetry
    context. Wrap the whole app with it, outside OpenTelemetry's middleware.

    Why it's needed: during a large upload, uvicorn pauses reading the socket
    and later resumes it from inside the request. asyncio then copies the
    request's context, including its active span, into the connection's read
    callback. Every later request on that keep-alive connection would start
    inside the old span and be recorded as part of the upload's trace.
    Resetting the context here keeps each request in a trace of its own.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        from opentelemetry import context

        token = context.attach(context.Context())
        try:
            await self.app(scope, receive, send)
        finally:
            context.detach(token)

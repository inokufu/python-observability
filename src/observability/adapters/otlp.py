from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from importlib.util import find_spec
from typing import override

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes

from observability.adapters.urls import origin
from observability.domain.contract import ContextValue, MonitoringContract

# Default names, then stable ones (OTEL_SEMCONV_STABILITY_OPT_IN). A WSGI
# instrumentation the service adds puts the query string in http.target.
_URL_KEYS = ("http.url", "http.target", "url.full")
_CLIENT_KEYS = (
    "net.peer.ip",
    "http.user_agent",
    "client.address",
    "user_agent.original",
)


def _scrubbed(span: ReadableSpan) -> ReadableSpan:
    """Remove query strings, outgoing paths and the client: they may hold an id."""
    attributes = dict(span.attributes or {})
    attributes.pop("url.query", None)
    for key in _URL_KEYS:
        url = attributes.get(key)
        if isinstance(url, str):
            attributes[key] = (
                origin(url)
                if span.kind == SpanKind.CLIENT
                else url.split(sep="?", maxsplit=1)[0]
            )
    if span.kind == SpanKind.SERVER:
        for key in _CLIENT_KEYS:
            attributes.pop(key, None)
    # A finished span is read-only, hence a copy.
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=span.events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class _ScrubbingSpanProcessor(BatchSpanProcessor):
    """Scrub every span on its way out, whatever instrumentation created it."""

    @override
    def on_end(self, span: ReadableSpan) -> None:
        super().on_end(span=_scrubbed(span))


class _UntracedPathSampler(Sampler):
    """Keep every span, whatever its caller decided, but those of untraced paths.

    The caller's sampling flag is client-controlled, and an error lives on its span.
    """

    def __init__(self, untraced_paths: frozenset[str]) -> None:
        self._untraced_paths = untraced_paths

    @override
    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        parent = trace.get_current_span(context=parent_context).get_span_context()
        attributes = attributes or {}
        # Default attribute name, then the stable one.
        path = attributes.get("http.target", attributes.get("url.path"))
        # A dropped request leaves an unsampled local parent to its SQL and HTTP spans.
        dropped = path in self._untraced_paths or (
            parent.is_valid and not parent.is_remote and not parent.trace_flags.sampled
        )
        return SamplingResult(
            decision=Decision.DROP if dropped else Decision.RECORD_AND_SAMPLE,
            attributes=attributes,
            trace_state=parent.trace_state,
        )

    @override
    def get_description(self) -> str:
        return "UntracedPathSampler"


class OtlpMonitoring(MonitoringContract):
    """Monitoring over OpenTelemetry, for any tool that accepts OTLP/HTTP."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None,
        service_name: str,
        environment: str,
        release: str,
        untraced_paths: list[str] | tuple[str, ...],
    ) -> None:
        """Set up the tracer provider and the outgoing HTTP instrumentation.

        Every span is kept, an unsampled caller included, as an error lives on its
        span. Untraced requests are dropped, with their SQL and HTTP spans.

        Args:
            endpoint: The OTLP/HTTP base URL, ``/v1/traces`` is appended.
            headers: Headers sent with every export, such as a token.
            service_name: The ``service.name`` resource attribute.
            environment: The ``deployment.environment.name`` resource attribute.
            release: The ``service.version`` resource attribute.
            untraced_paths: Request paths never measured, such as health checks.
        """
        self._provider = TracerProvider(
            resource=Resource.create(
                attributes={
                    "service.name": service_name,
                    "service.version": release,
                    "deployment.environment.name": environment,
                },
            ),
            sampler=_UntracedPathSampler(untraced_paths=frozenset(untraced_paths)),
        )
        self._provider.add_span_processor(
            _ScrubbingSpanProcessor(
                span_exporter=OTLPSpanExporter(
                    endpoint=f"{endpoint.rstrip('/')}/v1/traces",
                    headers=dict(headers or {}),
                ),
            ),
        )
        # Global, so the service's own spans also go through the scrubbing.
        trace.set_tracer_provider(self._provider)
        self._tracer = self._provider.get_tracer(instrumenting_module_name=__name__)
        # The otlp extra does not install httpx, and the instrumentor needs it.
        if find_spec("httpx") is not None:
            HTTPXClientInstrumentor().instrument(tracer_provider=self._provider)

    @override
    def report(self, exc: BaseException) -> None:
        span = trace.get_current_span()
        span.record_exception(exception=exc)
        # record_exception alone leaves the span's status unset.
        span.set_status(status=Status(status_code=StatusCode.ERROR))

    @override
    def set_context(self, **attributes: ContextValue) -> None:
        trace.get_current_span().set_attributes(attributes=attributes)

    @override
    @contextmanager
    def monitored_task(
        self,
        name: str,
        **attributes: ContextValue,
    ) -> Generator[None]:
        # The span records the exception that ends it, and marks itself failed.
        with self._tracer.start_as_current_span(name=name, attributes=attributes):
            yield

    @override
    def instrument_app(self, app: object) -> None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app=app,  # ty: ignore[invalid-argument-type]
            tracer_provider=self._provider,
            # ASGI adds a span per message, holding nothing the request span lacks.
            exclude_spans=["receive", "send"],
        )

    @override
    def instrument_engine(self, engine: object) -> None:
        from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer

        # Per engine: services import create_engine before SQLAlchemyInstrumentor runs.
        EngineTracer(
            tracer=self._provider.get_tracer(
                instrumenting_module_name="opentelemetry.instrumentation.sqlalchemy",
            ),
            # An AsyncEngine runs its queries through the sync engine it wraps.
            engine=getattr(engine, "sync_engine", engine),
            # EngineTracer requires it; it counts nothing without a meter provider.
            connections_usage=metrics.get_meter(name=__name__).create_up_down_counter(
                name="db.client.connections.usage",
            ),
        )

    @override
    def shutdown(self) -> None:
        self._provider.shutdown()

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import werkzeug
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from pytest_httpserver import HTTPServer
from sqlalchemy import Engine, StaticPool, create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from observability import MonitoringContract, NoopMonitoring, build_otlp_monitoring
from tests.conftest import (
    ENVIRONMENT,
    RELEASE,
    SERVICE,
    SERVICE_NAME,
    body_of,
    fresh_process_only,
    run_in_fresh_process,
    serve,
)


class OtlpCollector:
    """What an OTLP tool received, once the monitoring has been shut down."""

    def __init__(self, server: HTTPServer) -> None:
        """Answer every trace export and keep it."""
        self._server = server
        self.endpoint = f"http://{server.host}:{server.port}"
        server.expect_request("/v1/traces").respond_with_data("")

    def _requests(self) -> list[werkzeug.Request]:
        return [request for request, _ in self._server.log]

    def _exports(self) -> list[ExportTraceServiceRequest]:
        return [
            ExportTraceServiceRequest.FromString(body_of(request))
            for request in self._requests()
        ]

    def headers(self) -> list[dict[str, str]]:
        """The headers of each export received."""
        return [dict(request.headers) for request in self._requests()]

    def resources(self) -> list[dict[str, Any]]:
        """The resource attributes of each export received."""
        return [
            _attributes_of(resource_spans.resource)
            for export in self._exports()
            for resource_spans in export.resource_spans
        ]

    def server_spans(self) -> list[Span]:
        """The spans of the requests the service received."""
        return [span for span in self.spans() if span.kind == Span.SPAN_KIND_SERVER]

    def spans(self) -> list[Span]:
        """Every span received."""
        return [
            span
            for export in self._exports()
            for resource_spans in export.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]


def _attributes_of(element: Span | Span.Event | Resource) -> dict[str, Any]:
    """Read the attributes of a received span, resource or event as a dict."""
    return {
        attribute.key: getattr(
            attribute.value,
            attribute.value.WhichOneof("value"),  # ty: ignore[invalid-argument-type]
        )
        for attribute in element.attributes
    }


@pytest.fixture(autouse=True)
def uninstrument_httpx() -> Iterator[None]:
    """Remove the httpx instrumentation after each test: it is process-wide."""
    yield
    HTTPXClientInstrumentor().uninstrument()


@pytest.fixture
def collector(httpserver: HTTPServer) -> OtlpCollector:
    """A local OTLP tool."""
    return OtlpCollector(server=httpserver)


_HEADERS = {"Authorization": "Bearer ingest-token"}


@pytest.fixture
def monitoring(collector: OtlpCollector) -> Iterator[MonitoringContract]:
    """Monitoring with the default options, shut down after the test.

    Yields:
        The monitoring. A test shuts it down itself before reading the export.
    """
    monitoring = build_otlp_monitoring(
        **SERVICE,
        endpoint=collector.endpoint,
        headers=_HEADERS,
    )
    yield monitoring
    monitoring.shutdown()


@pytest.fixture
def engine() -> Iterator[Engine]:
    """An in-memory database, closed after the test.

    Yields:
        The engine.
    """
    # One shared connection: the route runs in another thread than the teardown.
    engine = create_engine(
        url="sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    yield engine
    engine.dispose()


@pytest.fixture
def client(monitoring: MonitoringContract, engine: Engine) -> TestClient:
    """The test service, its engine not yet measured."""
    return serve(monitoring=monitoring, engine=engine)


def _exception_types(span: Span) -> list[str]:
    return [
        _attributes_of(event)["exception.type"].rsplit(sep=".", maxsplit=1)[-1]
        for event in span.events
        if event.name == "exception"
    ]


class TestBuildOtlpMonitoring:
    """What the endpoint given turns into."""

    @pytest.mark.parametrize(
        argnames="endpoint",
        argvalues=["", None],
        ids=["empty", "none"],
    )
    def test_missing_endpoint_gives_noop(self, endpoint: str | None) -> None:
        """If the endpoint is empty or None, then monitoring should do nothing."""
        # Act
        monitoring = build_otlp_monitoring(**SERVICE, endpoint=endpoint)

        # Assert
        assert isinstance(monitoring, NoopMonitoring)


class TestOtlpExport:
    """How spans reach an OTLP tool."""

    def test_spans_are_described_and_authenticated(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
    ) -> None:
        """If spans are exported, then the export should carry service and token."""
        # Act
        client.get(url="/missing")
        monitoring.shutdown()

        # Assert
        resource = collector.resources()[0]
        assert resource["service.name"] == SERVICE_NAME
        assert resource["service.version"] == RELEASE
        assert resource["deployment.environment.name"] == ENVIRONMENT
        assert collector.headers()[0]["Authorization"] == _HEADERS["Authorization"]


class TestOtlpErrors:
    """What an OTLP tool receives as an error."""

    @pytest.mark.parametrize(
        argnames=("path", "recorded"),
        argvalues=[
            ("/fail", ["DomainError"]),
            ("/missing", []),
            ("/degraded", ["DomainError"]),
        ],
        ids=["server-error", "client-error", "caught-error"],
    )
    def test_error_is_recorded_when_server_side(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
        path: str,
        recorded: list[str],
    ) -> None:
        """If a route ends in an error, then only a 5xx or a report should be on it."""
        # Act
        client.get(url=path)
        monitoring.shutdown()

        # Assert
        [span] = collector.server_spans()
        assert _exception_types(span) == recorded
        assert (span.status.code == Status.STATUS_CODE_ERROR) == bool(recorded)

    def test_server_error_is_exported_under_an_unsampled_parent(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
    ) -> None:
        """If the caller did not sample its trace, then a 5xx should still be sent."""
        # Arrange
        unsampled = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"

        # Act
        client.get(url="/fail", headers={"traceparent": unsampled})
        monitoring.shutdown()

        # Assert
        assert _exception_types(collector.server_spans()[0]) == ["DomainError"]

    def test_context_is_attached(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
    ) -> None:
        """If a route sets a context, then its span should carry it."""
        # Act
        client.get(url="/users/42")
        monitoring.shutdown()

        # Assert
        attributes = _attributes_of(collector.server_spans()[0])
        assert attributes["user_id"] == 42
        assert attributes["organization_id"] == 7


class TestOtlpPersonalData:
    """What an OTLP tool never receives."""

    def test_request_target_query_is_not_sent(
        self,
        collector: OtlpCollector,
        monitoring: MonitoringContract,
    ) -> None:
        """If a span carries a request target, then its query should not be sent."""
        # Act
        with monitoring.monitored_task(name="sync"):
            trace.get_current_span().set_attribute(
                key="http.target",
                value="/users?email=jane%40example.com",
            )
        monitoring.shutdown()

        # Assert
        assert _attributes_of(collector.spans()[0])["http.target"] == "/users"

    def test_query_strings_are_not_sent(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
        downstream: Callable[[str], str],
    ) -> None:
        """If a URL is sent, then no query nor outgoing path should be."""
        # Arrange
        url = downstream("/geocode")

        # Act
        client.get(url="/call", params={"url": url + "?q=12+rue+de+la+Paix"})
        monitoring.shutdown()

        # Assert
        sent = str([_attributes_of(span) for span in collector.spans()])
        assert "Paix" not in sent
        assert "geocode" not in sent
        # Only the default attribute names carry the incoming URL whole.
        incoming = _attributes_of(collector.server_spans()[0]).get("http.url")
        assert incoming in {None, "http://testserver/call"}
        assert url.removesuffix("/geocode") in sent

    def test_client_identity_is_not_sent(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
    ) -> None:
        """If a request comes in, then no client IP or user agent should be sent."""
        # Act
        client.get(url="/missing", headers={"User-Agent": "Firefox/140"})
        monitoring.shutdown()

        # Assert
        sent = str(_attributes_of(collector.server_spans()[0]))
        assert "testclient" not in sent
        assert "Firefox" not in sent

    def test_stable_attribute_names_change_nothing(self) -> None:
        """If stable attribute names are opted in, then nothing should change."""
        # Act
        # OpenTelemetry reads the opt-in once per process.
        result = run_in_fresh_process(
            f"{__file__}::TestOtlpPersonalData::test_query_strings_are_not_sent",
            f"{__file__}::TestOtlpPersonalData::test_client_identity_is_not_sent",
            f"{__file__}::TestOtlpPerformance::test_untraced_path_is_not_measured",
            env={"OTEL_SEMCONV_STABILITY_OPT_IN": "http"},
        )

        # Assert
        assert result.returncode == 0, result.stdout
        assert "3 passed" in result.stdout


class TestOtlpPerformance:
    """What an OTLP tool receives as performance data."""

    def test_untraced_path_is_not_measured(
        self,
        collector: OtlpCollector,
        engine: Engine,
    ) -> None:
        """If a path is untraced, then neither it nor its queries should be measured."""
        # Arrange
        monitoring = build_otlp_monitoring(
            **SERVICE,
            endpoint=collector.endpoint,
            untraced_paths=["/query"],
        )
        monitoring.instrument_engine(engine=engine)
        client = serve(monitoring=monitoring, engine=engine)

        # Act
        client.get(url="/query")
        client.get(url="/degraded")
        monitoring.shutdown()

        # Assert
        assert [span.name for span in collector.spans()] == ["GET /degraded"]

    def test_sql_queries_are_measured(
        self,
        client: TestClient,
        monitoring: MonitoringContract,
        collector: OtlpCollector,
        engine: Engine,
    ) -> None:
        """If a route queries the database, then the query should get its own span."""
        # Arrange
        monitoring.instrument_engine(engine=engine)

        # Act
        client.get(url="/query")
        monitoring.shutdown()

        # Assert
        assert any(span.name.startswith("SELECT") for span in collector.spans())

    def test_async_sql_queries_are_measured(
        self,
        collector: OtlpCollector,
        monitoring: MonitoringContract,
    ) -> None:
        """If an async engine queries the database, then the query should be a span."""
        # Arrange
        engine = create_async_engine(url="sqlite+aiosqlite://")
        monitoring.instrument_engine(engine=engine)

        async def query() -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await engine.dispose()

        # Act
        with monitoring.monitored_task(name="sync"):
            asyncio.run(query())
        monitoring.shutdown()

        # Assert
        assert any(span.name.startswith("SELECT") for span in collector.spans())


class TestOtlpServiceSpans:
    """Spans the service creates itself."""

    def test_service_spans_hold_in_a_fresh_process(self) -> None:
        """If the service creates its own span, then it should be exported."""
        # Act
        # OpenTelemetry takes a global provider once per process.
        result = run_in_fresh_process(
            f"{__file__}::TestOtlpServiceSpans::test_service_spans_are_exported",
        )

        # Assert
        assert result.returncode == 0, result.stdout
        assert "1 passed" in result.stdout

    @fresh_process_only
    def test_service_spans_are_exported(
        self,
        collector: OtlpCollector,
        monitoring: MonitoringContract,
    ) -> None:
        """If the service creates its own span, then the tool should receive it."""
        # Arrange
        tracer = trace.get_tracer(instrumenting_module_name="billing")

        # Act
        with tracer.start_as_current_span(name="compute"):
            pass
        monitoring.shutdown()

        # Assert
        assert [span.name for span in collector.spans()] == ["compute"]


class TestOtlpTasks:
    """A task outside HTTP, reported to an OTLP tool."""

    def test_failed_task_is_recorded_and_raised(
        self,
        collector: OtlpCollector,
        monitoring: MonitoringContract,
    ) -> None:
        """If a task fails, then its span should record the error and raise."""
        # Act
        with (
            pytest.raises(RuntimeError),
            monitoring.monitored_task(name="sync", organization_id=3),
        ):
            raise RuntimeError("source unreachable")
        monitoring.shutdown()

        # Assert
        [span] = collector.spans()
        assert _attributes_of(span)["organization_id"] == 3
        assert span.status.code == Status.STATUS_CODE_ERROR
        assert _exception_types(span) == ["RuntimeError"]

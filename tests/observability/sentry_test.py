import json
import logging
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import Any

import pytest
import sentry_sdk
import werkzeug
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger
from pytest_httpserver import HTTPServer
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
from werkzeug import Response
from werkzeug.test import Client

from observability import (
    InvalidPropagationTargetError,
    MonitoringContract,
    NoopMonitoring,
    build_sentry_monitoring,
)
from tests.conftest import (
    ENVIRONMENT,
    RELEASE,
    SECRET,
    SERVICE,
    SERVICE_NAME,
    body_of,
    fresh_process_only,
    run_in_fresh_process,
    serve,
)


class SentryCollector:
    """What a Sentry-protocol tool received."""

    def __init__(self, server: HTTPServer) -> None:
        """Answer every envelope and keep its items."""
        self._items: list[tuple[str | None, Any]] = []
        self.dsn = f"http://public@{server.host}:{server.port}/1"
        server.expect_request("/api/1/envelope/").respond_with_handler(self._receive)

    def _receive(self, request: werkzeug.Request) -> Response:
        envelope = Envelope.deserialize(body_of(request))
        self._items.extend((item.type, item.payload.json) for item in envelope.items)
        return Response(status=HTTPStatus.OK)

    def payloads(self, *item_types: str) -> list[dict[str, Any]]:
        """Flush the SDK, then return the received items of the given types."""
        sentry_sdk.flush()
        return [payload for kind, payload in self._items if kind in item_types]

    def sent(self) -> str:
        """Flush the SDK, then return every event and transaction as one string."""
        return json.dumps(self.payloads("event", "transaction"))


@pytest.fixture(autouse=True)
def close_sdk() -> Iterator[None]:
    """Close the SDK after each test, as a service does at shutdown."""
    yield
    sentry_sdk.get_client().close()


@pytest.fixture
def collector(httpserver: HTTPServer) -> SentryCollector:
    """A local Sentry-protocol tool."""
    return SentryCollector(server=httpserver)


@pytest.fixture
def client(collector: SentryCollector) -> TestClient:
    """The test service, monitored with the default options."""
    return serve(monitoring=build_sentry_monitoring(**SERVICE, dsn=collector.dsn))


def _fail_a_task(monitoring: MonitoringContract) -> None:
    with (
        pytest.raises(RuntimeError),
        monitoring.monitored_task(name="sync", organization_id=3),
    ):
        raise RuntimeError("source unreachable")


def _collected(option: object) -> bool:
    """Whether a data_collection option is on: a flag, a list, or a nested mode."""
    if isinstance(option, dict):
        if "mode" in option:
            return option["mode"] != "off"
        return any(_collected(item) for item in option.values())
    return bool(option)


class TestBuildSentryMonitoring:
    """What the DSN given turns into."""

    @pytest.mark.parametrize(
        argnames="dsn",
        argvalues=["", None],
        ids=["empty", "none"],
    )
    def test_missing_dsn_gives_noop(self, dsn: str | None) -> None:
        """If the DSN is empty or None, then monitoring should do nothing."""
        # Act
        monitoring = build_sentry_monitoring(**SERVICE, dsn=dsn)

        # Assert
        assert isinstance(monitoring, NoopMonitoring)

    @pytest.mark.parametrize(
        argnames="dsn",
        argvalues=["", "http://public@localhost/1"],
        ids=["empty", "valid"],
    )
    def test_invalid_propagation_target_is_refused(self, dsn: str) -> None:
        """If a propagation target is not a valid regex, then building should fail."""
        # Act / Assert
        with pytest.raises(InvalidPropagationTargetError):
            build_sentry_monitoring(
                **SERVICE,
                dsn=dsn,
                trace_propagation_targets=["^https://b("],
            )


class TestSentryErrors:
    """What reaches a Sentry-protocol tool as an error."""

    @pytest.mark.parametrize(
        argnames=("path", "reported"),
        argvalues=[
            ("/fail", ["DomainError"]),
            ("/missing", []),
            ("/degraded", ["DomainError"]),
        ],
        ids=["server-error", "client-error", "caught-error"],
    )
    def test_error_is_reported_when_server_side(
        self,
        client: TestClient,
        collector: SentryCollector,
        path: str,
        reported: list[str],
    ) -> None:
        """If a route ends in an error, then only a 5xx or a report should be sent."""
        # Act
        client.get(url=path)

        # Assert
        events = collector.payloads("event")
        assert [event["exception"]["values"][0]["type"] for event in events] == reported

    def test_context_is_attached(
        self,
        client: TestClient,
        collector: SentryCollector,
    ) -> None:
        """If a route sets a context, then its error should carry it and the release."""
        # Act
        client.get(url="/users/42")

        # Assert
        event = collector.payloads("event")[0]
        assert event["tags"]["user_id"] == "42"
        assert event["tags"]["organization_id"] == "7"
        assert event["tags"]["service"] == SERVICE_NAME
        assert event["environment"] == ENVIRONMENT
        assert event["release"] == RELEASE

    def test_personal_data_is_not_sent(
        self,
        client: TestClient,
        collector: SentryCollector,
    ) -> None:
        """If a request carries personal data, then none of it should be sent."""
        # Act
        client.get(
            url="/users/42",
            params={"email": "jane@example.com"},
            headers={"X-Learner": "Jane Doe"},
        )

        # Assert
        sent = collector.sent()
        assert "DomainError" in sent
        assert "jane@example.com" not in sent
        assert "Jane Doe" not in sent
        assert SECRET not in sent

    def test_every_data_category_is_off(self, collector: SentryCollector) -> None:
        """If the SDK knows a data category, then only source lines should be on."""
        # Arrange
        build_sentry_monitoring(**SERVICE, dsn=collector.dsn)

        # Act
        options = dict(sentry_sdk.get_client().options["data_collection"])
        options.pop("provided_by_user")
        source_lines = options.pop("frame_context_lines")

        # Assert
        assert source_lines
        assert [key for key, value in options.items() if _collected(value)] == []

    def test_outgoing_query_strings_are_not_sent(
        self,
        client: TestClient,
        collector: SentryCollector,
        downstream: Callable[[str], str],
    ) -> None:
        """If an outgoing URL carries a query string, then nothing should contain it."""
        # Act
        client.get(
            url="/geocode",
            params={"url": downstream("/search") + "?q=12+rue+de+la+Paix"},
        )

        # Assert
        sent = collector.sent()
        assert "Paix" not in sent
        assert "/search" in sent

    @pytest.mark.parametrize(
        argnames="initialised_before",
        argvalues=[False, True],
        ids=["first-init", "after-another-init"],
    )
    def test_log_records_are_not_reported(
        self,
        collector: SentryCollector,
        initialised_before: bool,
    ) -> None:
        """If the service logs an error, then it should not become an event."""
        # Arrange
        if initialised_before:
            build_sentry_monitoring(
                **SERVICE,
                dsn=collector.dsn,
                log_breadcrumbs=True,
            ).shutdown()
        build_sentry_monitoring(**SERVICE, dsn=collector.dsn)

        # Act
        logging.getLogger("service").error("Payment failed")
        loguru_logger.error("Payment failed")

        # Assert
        assert collector.payloads("event") == []

    def test_log_breadcrumbs_hold_in_a_fresh_process(self) -> None:
        """If log breadcrumbs are on at the first init, then logs should be crumbs."""
        # Act
        # Loguru's integration fixes its handlers at the first init in the process, and
        # the other tests turn them off.
        result = run_in_fresh_process(
            f"{__file__}::TestSentryErrors::test_log_records_become_breadcrumbs_when_asked",
        )

        # Assert
        assert result.returncode == 0, result.stdout
        assert "1 passed" in result.stdout

    @fresh_process_only
    def test_log_records_become_breadcrumbs_when_asked(
        self,
        collector: SentryCollector,
    ) -> None:
        """If log breadcrumbs are on, then logs should precede an error, not be one."""
        # Arrange
        monitoring = build_sentry_monitoring(
            **SERVICE,
            dsn=collector.dsn,
            log_breadcrumbs=True,
        )

        # Act
        logging.getLogger("service").error("Stdlib step")
        loguru_logger.error("Loguru step")
        monitoring.report(exc=ValueError("lookup failed"))

        # Assert
        events = collector.payloads("event")
        assert len(events) == 1
        crumbs = str(
            [crumb.get("message") for crumb in events[0]["breadcrumbs"]["values"]],
        )
        assert "Stdlib step" in crumbs
        assert "Loguru step" in crumbs


class TestSentryPerformance:
    """What reaches a Sentry-protocol tool as performance data."""

    def test_untraced_path_is_not_measured(self, collector: SentryCollector) -> None:
        """If a path is untraced, then only its errors should be sent."""
        # Arrange
        client = serve(
            monitoring=build_sentry_monitoring(
                **SERVICE,
                dsn=collector.dsn,
                untraced_paths=["/fail"],
            ),
        )

        # Act
        client.get(url="/fail")
        client.get(url="/degraded")

        # Assert
        transactions = collector.payloads("transaction")
        assert [transaction["transaction"] for transaction in transactions] == [
            "/degraded",
        ]
        errors = [
            event["exception"]["values"][0]["value"]
            for event in collector.payloads("event")
        ]
        assert "database down" in errors

    def test_untraced_path_is_not_measured_in_a_wsgi_app(
        self,
        collector: SentryCollector,
    ) -> None:
        """If a WSGI path is untraced, then only the other paths should be measured."""
        # Arrange
        build_sentry_monitoring(
            **SERVICE,
            dsn=collector.dsn,
            untraced_paths=["/health"],
        )

        def app(
            _environ: dict[str, Any],
            start_response: Callable[..., Any],
        ) -> list[bytes]:
            start_response("200 OK", [])
            return [b"ok"]

        client = Client(SentryWsgiMiddleware(app=app))

        # Act
        client.get("/health")
        client.get("/work")

        # Assert
        transactions = collector.payloads("transaction")
        urls = [transaction["request"]["url"] for transaction in transactions]
        assert urls == ["http://localhost/work"]

    def test_route_is_measured_under_an_unsampled_parent(
        self,
        client: TestClient,
        collector: SentryCollector,
    ) -> None:
        """If the caller did not sample its trace, then the route should be measured."""
        # Arrange
        unsampled = "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0"

        # Act
        client.get(url="/missing", headers={"sentry-trace": unsampled})

        # Assert
        assert collector.payloads("transaction")

    def test_trace_headers_reach_listed_hosts_only(
        self,
        collector: SentryCollector,
        downstream: Callable[[str], str],
        httpserver_ipv4: HTTPServer,
    ) -> None:
        """If a route calls out, then only listed targets should get trace headers."""
        # Arrange
        client = serve(
            monitoring=build_sentry_monitoring(
                **SERVICE,
                dsn=collector.dsn,
                trace_propagation_targets=["/internal"],
            ),
        )

        # Act
        client.get(url="/call", params={"url": downstream("/internal/users")})
        client.get(url="/call", params={"url": downstream("/external/geocode")})

        # Assert
        received = {request.path: request.headers for request, _ in httpserver_ipv4.log}
        assert "sentry-trace" in received["/internal/users"]
        assert "sentry-trace" not in received["/external/geocode"]
        assert "baggage" not in received["/external/geocode"]


class TestSentryTasks:
    """A task outside HTTP, reported to a Sentry-protocol tool."""

    def test_failed_task_is_reported(self, collector: SentryCollector) -> None:
        """If a task fails, then its error and its run should carry its context."""
        # Act
        _fail_a_task(monitoring=build_sentry_monitoring(**SERVICE, dsn=collector.dsn))

        # Assert
        event = collector.payloads("event")[0]
        transaction = collector.payloads("transaction")[0]
        assert event["tags"]["organization_id"] == "3"
        assert transaction["transaction"] == "sync"

    def test_context_outside_a_task_is_ignored(
        self,
        collector: SentryCollector,
    ) -> None:
        """If context is set outside a request or task, then it should be ignored."""
        # Arrange
        monitoring = build_sentry_monitoring(**SERVICE, dsn=collector.dsn)

        # Act
        monitoring.set_context(organization_id=3)
        with pytest.raises(RuntimeError), monitoring.monitored_task(name="sync"):
            raise RuntimeError("source unreachable")

        # Assert
        event = collector.payloads("event")[0]
        assert "organization_id" not in event["tags"]

    def test_command_line_is_not_sent(self, collector: SentryCollector) -> None:
        """If a task fails, then the process command line should not be sent."""
        # Act
        _fail_a_task(monitoring=build_sentry_monitoring(**SERVICE, dsn=collector.dsn))

        # Assert
        event = collector.payloads("event")[0]
        assert "sys.argv" not in event.get("extra", {})

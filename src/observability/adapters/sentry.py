import logging
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from importlib.util import find_spec
from typing import Any, override

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.argv import ArgvIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from observability.adapters.urls import origin
from observability.domain.contract import ContextValue, MonitoringContract

# Each of these is collected by default and may hold personal data.
_DATA_COLLECTION = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_headers": {"request": {"mode": "off"}},
    "http_bodies": [],
    "url_query_params": {"mode": "off"},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
    # The service's own code around each frame: kept, it holds no personal data.
    "frame_context_lines": 5,
}


def _scrub_outgoing(data: dict[str, Any] | None) -> None:
    # Outgoing HTTP spans and breadcrumbs keep the URL whatever data_collection says.
    if not data:
        return
    data.pop("http.query", None)
    if isinstance(data.get("url"), str):
        data["url"] = origin(data["url"])


def _scrub_event(event: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
    # A message may quote the data that failed, such as a pydantic input.
    for exception in event.get("exception", {}).get("values", []):
        exception.pop("value", None)
    return event


def _scrub_transaction(event: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
    for span in event.get("spans", []):
        _scrub_outgoing(span.get("data"))
        description = span.get("description")
        # Other clients share the op, such as boto3 with "aws.s3.GetObject".
        if span.get("op") == "http.client" and isinstance(description, str):
            method, _space, url = description.partition(" ")
            if url:
                span["description"] = f"{method} {origin(url)}"
    return event


def _scrub_breadcrumb(crumb: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
    _scrub_outgoing(crumb.get("data"))
    return crumb


def _sample(context: dict[str, Any], untraced_paths: frozenset[str]) -> float:
    """Measure every request but the untraced ones, whatever its caller decided.

    The caller's flag is client-controlled; ignoring it keeps the transaction behind
    each error.
    """
    # The SDK hands the request as an ASGI scope or a WSGI environ, by framework.
    scope = context.get("asgi_scope") or {}
    environ = context.get("wsgi_environ") or {}
    path = scope.get("path", environ.get("PATH_INFO"))
    return 0.0 if path in untraced_paths else 1.0


def _log_integrations(*, as_breadcrumbs: bool) -> list[Integration]:
    """Log records as breadcrumbs, or not at all, and never as events.

    Passed explicitly rather than disabled: the SDK reads disabled_integrations only
    at its first init in the process.
    """
    level = logging.INFO if as_breadcrumbs else None
    integrations: list[Integration] = [
        LoggingIntegration(level=level, event_level=None),
    ]
    if find_spec("loguru") is not None:
        from sentry_sdk.integrations.loguru import LoguruIntegration

        integrations.append(LoguruIntegration(level=level, event_level=None))
    return integrations


class SentryMonitoring(MonitoringContract):
    """Monitoring over the Sentry protocol, for any tool that accepts a DSN."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        dsn: str,
        service_name: str,
        environment: str,
        release: str,
        trace_propagation_targets: list[str] | tuple[str, ...],
        untraced_paths: list[str] | tuple[str, ...],
        log_breadcrumbs: bool,
    ) -> None:
        """Start the SDK, measuring every request but the untraced ones.

        Args:
            dsn: Where to send.
            service_name: Tagged on every event as ``service``.
            environment: The deployment environment.
            release: The service version, sent as ``<service_name>@<release>``.
            trace_propagation_targets: Regular expressions searched in each outgoing
                URL. Only a matching request receives trace headers.
            untraced_paths: Request paths never measured, such as health checks.
            log_breadcrumbs: Attach log records to the next event as breadcrumbs.
        """
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            # GlitchTip and Sentry share a release across an organization's projects.
            release=f"{service_name}@{release}",
            traces_sampler=partial(_sample, untraced_paths=frozenset(untraced_paths)),
            trace_propagation_targets=list(trace_propagation_targets),
            data_collection=_DATA_COLLECTION,  # ty: ignore[invalid-argument-type]
            before_send=_scrub_event,  # ty: ignore[invalid-argument-type]
            before_send_transaction=_scrub_transaction,  # ty: ignore[invalid-argument-type]
            before_breadcrumb=_scrub_breadcrumb,
            # The command line may hold secrets.
            disabled_integrations=[ArgvIntegration],
            integrations=_log_integrations(as_breadcrumbs=log_breadcrumbs),
        )
        sentry_sdk.get_global_scope().set_tag(key="service", value=service_name)

    @override
    def report(self, exc: BaseException) -> None:
        sentry_sdk.capture_exception(exc)

    @override
    def set_context(self, **attributes: ContextValue) -> None:
        # Outside a request or a task, a tag would land on every later event.
        if sentry_sdk.get_current_span() is None:
            return
        sentry_sdk.set_tags(tags={key: str(value) for key, value in attributes.items()})

    @override
    @contextmanager
    def monitored_task(
        self,
        name: str,
        **attributes: ContextValue,
    ) -> Generator[None]:
        # Its own scope, so the task's tags do not stay on later events.
        with (
            sentry_sdk.isolation_scope(),
            sentry_sdk.start_transaction(op="task", name=name),
        ):
            self.set_context(**attributes)
            try:
                yield
            except Exception as e:
                self.report(exc=e)
                raise

    @override
    def instrument_app(self, app: object) -> None:
        # The SDK instruments the app on its own.
        pass

    @override
    def instrument_engine(self, engine: object) -> None:
        # The SDK instruments every engine on its own.
        pass

    @override
    def shutdown(self) -> None:
        sentry_sdk.get_client().close()

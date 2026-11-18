import re
from collections.abc import Mapping

from observability.adapters.noop import NoopMonitoring
from observability.domain.contract import MonitoringContract
from observability.domain.errors import InvalidPropagationTargetError


def build_sentry_monitoring(  # noqa: PLR0913
    *,
    service_name: str,
    environment: str,
    release: str,
    dsn: str | None,
    trace_propagation_targets: list[str] | tuple[str, ...] = (),
    untraced_paths: list[str] | tuple[str, ...] = (),
    log_breadcrumbs: bool = False,
) -> MonitoringContract:
    r"""Build monitoring over the Sentry protocol, for any tool that accepts a DSN.

    Call it once per process, then hand it the app and the engine to measure with
    :meth:`MonitoringContract.instrument_app` and
    :meth:`MonitoringContract.instrument_engine`. Every request is measured but the
    untraced paths, whatever sampling decision its caller sent.

    Args:
        service_name: The service, as the tool should name it.
        environment: The deployment environment.
        release: The service version.
        dsn: Where to send. Empty or ``None``, monitoring does nothing.
        trace_propagation_targets: Regular expressions searched in each outgoing
            URL; only a matching request receives trace headers. Anchor them, such
            as ``r"^https://billing\.internal/"``.
        untraced_paths: Request paths never measured, such as ``"/health"``. Their
            errors are still reported.
        log_breadcrumbs: Attach the service's log records, ``INFO`` and above, to
            the next event as breadcrumbs, with their extra fields. They never
            become events. Leave it off when logs may hold personal data.

    Returns:
        The monitoring, which does nothing without a DSN.

    Raises:
        InvalidPropagationTargetError: A propagation target is not a valid regular
            expression.
    """
    # Checked even without a DSN, so development fails as production would.
    for target in trace_propagation_targets:
        try:
            re.compile(target)
        except re.error as e:
            raise InvalidPropagationTargetError from e
    if not dsn:
        return NoopMonitoring()
    from observability.adapters.sentry import SentryMonitoring

    return SentryMonitoring(
        dsn=dsn,
        service_name=service_name,
        environment=environment,
        release=release,
        trace_propagation_targets=trace_propagation_targets,
        untraced_paths=untraced_paths,
        log_breadcrumbs=log_breadcrumbs,
    )


def build_otlp_monitoring(  # noqa: PLR0913
    *,
    service_name: str,
    environment: str,
    release: str,
    endpoint: str | None,
    headers: Mapping[str, str] | None = None,
    untraced_paths: list[str] | tuple[str, ...] = (),
) -> MonitoringContract:
    """Build monitoring over OpenTelemetry, for any tool that accepts OTLP/HTTP.

    Call it once per process, then hand it the app and the engine to measure with
    :meth:`MonitoringContract.instrument_app` and
    :meth:`MonitoringContract.instrument_engine`. Every request is measured but the
    untraced paths, whatever sampling decision its caller sent.

    Args:
        service_name: The service, as the tool should name it.
        environment: The deployment environment.
        release: The service version.
        endpoint: The OTLP/HTTP base URL, such as ``https://otlp.example``;
            ``/v1/traces`` is appended. Empty or ``None``, monitoring does nothing.
        headers: Headers sent with every export, such as a token.
        untraced_paths: Request paths never measured, such as ``"/health"``. Having
            no span, their errors are not recorded either.

    Returns:
        The monitoring, which does nothing without an endpoint.
    """
    if not endpoint:
        return NoopMonitoring()
    from observability.adapters.otlp import OtlpMonitoring

    return OtlpMonitoring(
        endpoint=endpoint,
        headers=headers,
        service_name=service_name,
        environment=environment,
        release=release,
        untraced_paths=untraced_paths,
    )

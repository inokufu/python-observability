# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-17

### Added

- `MonitoringContract`, with `report`, `record_server_error`, `set_context`,
  `monitored_task`, `instrument_app`, `instrument_engine` and `shutdown`.
- `build_sentry_monitoring`, with a DSN, and `build_otlp_monitoring`, with an OTLP
  endpoint. An empty or `None` destination gives `NoopMonitoring`. An invalid
  propagation target raises `InvalidPropagationTargetError`.
- `untraced_paths` on both builders, for paths never measured such as health checks.
- `log_breadcrumbs` on `build_sentry_monitoring`: log records, with their extra fields,
  precede an event as breadcrumbs, and never become one.
- The `sentry` extra: monitoring over the Sentry protocol.
- The `otlp` extra: monitoring over OpenTelemetry, exported as OTLP/HTTP. It
  measures FastAPI, httpx and SQLAlchemy engines, synchronous or async, and
  sets its tracer provider as the global one.
- No personal data collected by default in either format: no headers, bodies, query
  strings, local variables, command line, client IP or user agent.

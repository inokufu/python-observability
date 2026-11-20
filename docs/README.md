# Observability

[![Python](https://img.shields.io/badge/Python-FFD43B?logo=python)](https://www.python.org/)
![License](https://img.shields.io/badge/GPL--3.0-red?logo=gnu)
[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-E6F0FF?logo=githubactions)](https://github.com/features/actions)
[![Pytest](https://img.shields.io/badge/pytest-E6F7FF?logo=pytest)](https://docs.pytest.org/)
[![EditorConfig](https://img.shields.io/badge/EditorConfig-333333?logo=editorconfig)](https://editorconfig.org/)
[![uv](https://img.shields.io/badge/uv-261230?logo=astral)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/Ruff-3A3A3A?logo=ruff)](https://docs.astral.sh/ruff/)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![Prek](https://img.shields.io/badge/prek-F54327?logo=prek)](https://prek.j178.dev/)
[![Makefile](https://img.shields.io/badge/Makefile-427819?logo=gnu)](https://www.gnu.org/software/make/manual/make.html)
[![MkDocs](https://img.shields.io/badge/MkDocs-526CFE?logo=markdown)](https://www.mkdocs.org/)
[![Dependabot](https://img.shields.io/badge/Dependabot-025E8C?logo=dependabot)](https://docs.github.com/code-security/dependabot)
[![gitleaks](https://img.shields.io/badge/gitleaks-C0392B)](https://github.com/gitleaks/gitleaks)
[![zizmor](https://img.shields.io/badge/zizmor-6E40C9)](https://docs.zizmor.sh/)

## Overview

This library gives a service error and performance monitoring through one
contract, whatever tool receives the data. It speaks either of the two standard
formats, and the service picks one:

| The service calls          | With             | The library uses | Tools                       |
|----------------------------|------------------|------------------|-----------------------------|
| `build_sentry_monitoring`  | a DSN            | the Sentry SDK   | Sentry, GlitchTip, Bugsink… |
| `build_otlp_monitoring`    | an OTLP endpoint | OpenTelemetry    | SigNoz, Uptrace, Elastic…   |

An empty destination gives a monitoring that does nothing.

The Sentry SDK detects the web framework, the HTTP clients and the database on its
own. The OTLP format measures a FastAPI app, httpx clients and SQLAlchemy engines.
Both cover errors and traces, not metrics.

Key features:

- 🔌 One contract, `MonitoringContract`, so the service never depends on an SDK
- 🚦 Server errors reported: an unhandled exception, a 5xx or a failed task, never
  a 4xx. Any other caught exception when the service asks
- 🔒 No personal data collected by default: no headers, bodies, query strings,
  local variables or command line (see What each format sends for what still gets
  through)
- 🧭 Trace headers sent only to the URLs you list (Sentry format)
- 📏 Every request measured, whatever sampling decision its caller sent, but the
  paths you leave out, such as health checks (see Limits for the Sentry SDK under
  load)
- 📝 Log records never become events: logging stays separate from monitoring. They
  can precede an error as breadcrumbs, if you ask (Sentry format)
- 🧪 Tested against local servers standing in for each kind of tool

## Setup and installation

### Prerequisites

- Python 3.13 or higher
- [uv](https://docs.astral.sh/uv/) for dependency management

### Installation

Install the extra matching your destination:

```sh
uv add "observability[sentry]"   # a DSN
uv add "observability[otlp]"     # an OTLP endpoint
```

## Usage

### Build the monitoring

Build it once per process, then hand it the app right after creating it:

With a DSN:

```python
from observability import build_sentry_monitoring

monitoring = build_sentry_monitoring(
    service_name="billing",
    environment="staging",
    release="1.2.3",
    dsn=settings.monitoring_dsn,
    trace_propagation_targets=[r"^https://billing\.internal/"],
    untraced_paths=["/health", "/ready"],
)
monitoring.instrument_app(app=app)
```

With an OTLP endpoint:

```python
from observability import build_otlp_monitoring

monitoring = build_otlp_monitoring(
    service_name="billing",
    environment="staging",
    release="1.2.3",
    endpoint=settings.otlp_endpoint,
    headers={"Authorization": f"Bearer {settings.otlp_token}"},
    untraced_paths=["/health", "/ready"],
)
monitoring.instrument_app(app=app)
```

Each builder imports its SDK only when called with a destination, so the other
extra need not be installed. The OTLP endpoint is the base URL: `/v1/traces` is
appended.

`release` takes the version alone. The Sentry format sends it as
`<service_name>@<release>`, `billing@1.2.3` here, since an organization's projects
share their releases. The OTLP format sends it as `service.version`.

Hand it the database engine right after creating it, synchronous or async:

```python
engine = create_engine(url)
monitoring.instrument_engine(engine=engine)
```

An empty or `None` DSN or endpoint gives a monitoring that does nothing, so the
same code runs in development. A DSN the Sentry SDK cannot parse stops the
service at startup. Nothing tells you a DSN was
left empty in production: if that matters, your deployment has to check it.

The library reads no environment variable: every value comes as a parameter. The
SDKs still honour their own (`SENTRY_*`, `OTEL_*`), which belong to whoever deploys
the service.

`trace_propagation_targets` holds regular expressions, searched in the full URL of
each outgoing request. An invalid one raises `InvalidPropagationTargetError` at
startup, even with an empty DSN. Anchor them: `billing.internal` would also match
`https://evil.example/?next=billing-internal`.

`untraced_paths` lists the exact request paths never measured, their SQL and HTTP
calls included, such as health checks a platform probes every few seconds. Their
errors are still reported in the Sentry format; in the OTLP format they are not,
having no span.

`log_breadcrumbs=True` attaches the service's log records, `INFO` and above, to the
next Sentry event, so an error arrives with what led to it. They still never become
events. Leave it off when logs may hold personal data: the record's message and its
extra fields (loguru's `extra`, the `extra` of `logging`) are sent as they are. So
is the URL httpx logs for each request at `INFO`, path and query included.

### Report a server error

The SDKs do not see an exception that a handler turned into a response. Call
`record_server_error` in the handlers of the service's own exceptions:

```python
monitoring.record_server_error(exc=exc, status_code=status_code)
```

Only a 5xx is reported. Don't call it from a catch-all `Exception` handler:
Starlette raises that exception again, and the SDKs record it on their own. The
OTLP format would record it twice.

An `HTTPException` with a 5xx needs no call: the Sentry format reports it on its
own, and the OTLP format marks the request span as an error, without the exception.

### Report a caught error

An exception the service catches and recovers from is not reported, whatever it
answers. Call `report` when you want to know about it, such as a degraded answer:

```python
try:
    rate = fetch_rate(currency=currency)
except RateUnavailableError as e:
    monitoring.report(exc=e)
    rate = DEFAULT_RATE
```

Call it inside a request or a `monitored_task`. Elsewhere, the Sentry format still
sends it, while the OTLP format, having no span to record it on, drops it.

### Attach context

```python
monitoring.set_context(user_id=user_id, organization_id=organization_id)
```

Only technical identifiers belong here, never a name or an email. Call it inside a
request or a `monitored_task`; elsewhere it does nothing, since a tag set outside
would land on every later event.

In the OTLP format, call it outside any span the service opens: it sets the current
span, so the request's would not carry it.

### Monitor a task outside HTTP

```python
with monitoring.monitored_task(name="sync", organization_id=organization_id):
    run_sync()
```

The task is measured, and the exception that ends it is reported, then raised.
So don't `report` it before raising it again: the OTLP format would record it twice.
Use it outside a request only: within one, the Sentry format starts a separate
trace.

### Shut down

```python
monitoring.shutdown()
```

It sends what is pending. At process exit, both formats also flush on their own.

### Test a service

Depend on the contract and mock it:

```python
from unittest.mock import MagicMock

from observability import MonitoringContract

monitoring = MagicMock(spec=MonitoringContract)
```

### What each format sends

| | DSN (Sentry SDK) | OTLP endpoint (OpenTelemetry) |
|---|---|---|
| Errors | Grouped events, with stack traces | Exceptions recorded on the span |
| Requests | Transactions per route | Server spans per route, without ASGI `receive` and `send` spans |
| Outgoing HTTP | Spans from the clients the SDK detects | httpx spans; httpx2 calls are not measured |
| SQL | SQLAlchemy spans | SQLAlchemy spans, once `instrument_engine` is called |
| Spans the service creates | Through `sentry_sdk`, outside the contract | Through `trace.get_tracer()`, scrubbed like the rest |
| Trace headers | Only to URLs matching `trace_propagation_targets` | `traceparent`, and any incoming `baggage`, to every outgoing request |
| Query strings | Not collected, and removed from outgoing spans and breadcrumbs | Removed from every URL |
| Outgoing URLs | Host only, in spans and breadcrumbs | Host only, on client spans |
| Exception messages | Not sent: the type and stack trace are | Sent |
| Client IP, user agent | Not collected | Removed from server spans |

A URL naming no host, such as `/next`, keeps its path without its query.

`traceparent` only carries random identifiers, and `baggage` is the caller's own
data. The OpenTelemetry httpx instrumentation offers no way to restrict them per host.

An exception reported without being raised has no stack trace. Its message being
dropped, the Sentry format can then only group it by type: raise it first, or give
each failure its own type.

Still sent as they are:

- **Incoming URL paths**, in both formats: keep personal data out of them, such as
  an email in `/users/by-email/...`.
- **Exception messages (OTLP)**, in the span event and its stack trace. A pydantic
  error quotes the input it refused, and an httpx error its URL.
- **Command lines (Sentry).** A subprocess breadcrumb and span carry the whole
  command: pass a password through the environment, not arguments.
- **SQL error messages (OTLP).** A failed query sends the database's error message
  in its span status, and a PostgreSQL constraint error quotes the values.

### Setup rules

- **Set up logging first.** A `loguru.logger.remove()` called once the monitoring is
  built also removes the breadcrumb handler, silently.
- **Initialise Sentry only through the library.** The SDK sets its integrations up
  at its first init: if other code initialised Sentry before, its loguru handlers and
  its command-line capture stay.
- **Call `instrument_app` before the app starts (OTLP).** Called from the lifespan or
  after a first request, it adds nothing: FastAPI's own telemetry measures the app
  instead, adding spans for its dependencies, endpoint and serialization.
- **Let the library own OpenTelemetry (OTLP).** Don't run `opentelemetry-instrument`,
  set a tracer provider of your own, or add a span processor: OpenTelemetry keeps the
  first global provider, and spans that reach another provider or processor bypass
  the scrubbing. An instrumentation the service adds goes through the library's
  provider.
- **Run background work in a `monitored_task` (OTLP).** Outside a request or a task,
  every outgoing HTTP call and SQL query becomes a trace of its own.
- **Leave `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` unset (OTLP).** It would
  export the headers it names, `Authorization` and `Cookie` included.

### Limits

- **A full send queue drops data (Sentry).** The SDK queues up to 100 envelopes,
  lowers its trace sampling as the queue fills, and drops what does not fit, errors
  included. In a burst of 300 requests, about two thirds of the transactions were
  dropped.
- **A full export queue drops spans (OTLP).** The batch processor holds 2048 spans
  by default and drops the overflow, error spans included, which is most likely
  during a burst of 5xx.

## Development

`make help` lists every command.

### Code Formatting and Linting

```sh
make format     # Ruff format
make lint       # Ruff lint
make typecheck  # ty
make test       # pytest with coverage, refused below 90 %
make diff-cover # coverage of the changed lines, refused below 90 %
make check      # all of the above
```

`make install` installs both extras, so the tests cover both formats.

### Git Hooks

`make install` installs them in a git repository, but `git commit --no-verify` skips
them. On GitHub, require the `Lint` and `Test Python` checks on `main`,
and enable secret push protection.

### Vocabulary

- **DSN**: the URL a Sentry-protocol tool gives a project, holding its key.
- **OTLP**: OpenTelemetry's export protocol, here over HTTP.
- **Trace**: one operation across services, made of **spans**, each a timed step
  such as a request, a query or an HTTP call. The **server span** is the request a
  service received; Sentry calls it a **transaction**.
- **Tag**: a key and value attached to an event, used to filter.
- **Breadcrumb**: a past step, such as a log record, sent with the next error.
- **Propagation**: the trace headers sent to the next service: `sentry-trace` or
  `traceparent`, plus `baggage`.
- **Tracer provider**: the OpenTelemetry object that creates spans; its **span
  processors** export them.

### Architecture

```
src/observability/
├── domain/            # the port, free of any SDK
│   ├── contract.py    # MonitoringContract
│   └── errors.py      # ObservabilityError and its subclasses
├── adapters/          # one adapter per format
│   ├── noop.py        # no destination
│   ├── sentry.py      # the `sentry` extra
│   ├── otlp.py        # the `otlp` extra
│   └── urls.py        # what both formats keep of an outgoing URL
└── builders.py        # build_sentry_monitoring, build_otlp_monitoring
```

The 5xx rule lives in the contract, in `record_server_error`, so every adapter
applies it. An adapter only sends. Adding a format means adding an adapter.

### Design decisions

- **Every request is measured, whatever its caller sent.** The caller's sampling
  flag is client-controlled, and an error belongs with its request. Untraced paths
  are the one exception.
- **A log record never becomes an error.** A log level does not decide what is one.
- **No SDK option passes through.** A service cannot turn personal data back on.
- **No environment variable is read.** The SDKs' own belong to whoever deploys.
- **The app and the engine are passed in.** Services create them before the
  monitoring exists.

### Tests

Each format runs against a local HTTP server standing in for the monitoring tool, so
a test reads what the library actually sends. A test builds the monitoring as a
service would, and `serve` in `tests/conftest.py` gives it the test service. Only
this library's logic is tested, and a safeguard is proven by breaking it. Settings
an SDK reads once per process are tested in a fresh interpreter.

## Publishing a version

A version is a git **tag** `vX.Y.Z` on `main`. Consumers pin that tag.

1. Describe the changes in `docs/CHANGELOG.md`, with what each consumer must adapt.
2. Bump `version` in `pyproject.toml`, run `uv lock`, then commit both files.
3. Create and push the tag `vX.Y.Z`.
4. In each consumer: change the tag in `[tool.uv.sources]`, run
   `uv lock --upgrade-package observability`, then commit the `uv.lock`.

**Never move an existing tag.** A consumer that already locked it keeps the old
commit in its `uv.lock`, while a fresh lock would take the new one: two builds of
the same tag would no longer run the same code. For a fix, publish a new tag.

Changing `MonitoringContract` or a builder, or the data either format
sends, is a major version: every consumer depends on them.

## Contributing

We welcome contributions to this project! Please see
the [CONTRIBUTING.md](CONTRIBUTING.md) file for guidelines on how to
contribute, including:

- How to set up your development environment
- Coding standards and style guidelines
- Pull request process
- Testing requirements

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0) -
see the LICENSE file for details.

GPL-3.0 is a strong copyleft license that requires anyone who distributes your
code or a derivative work to make the source available under the same terms.

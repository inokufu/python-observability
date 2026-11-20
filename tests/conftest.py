"""What both formats' tests share: the service they call, and how to monitor it.

Each format is tested against a local HTTP server standing in for the monitoring
tool, so the tests read what the lib actually sends.
"""

import gzip
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import TypedDict

import httpx
import pytest
import werkzeug
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pytest_httpserver import HTTPServer
from sqlalchemy import Engine, text

from observability import MonitoringContract

FRESH_PROCESS = "OBSERVABILITY_FRESH_PROCESS"
fresh_process_only = pytest.mark.skipif(
    condition=FRESH_PROCESS not in os.environ,
    reason="needs a fresh interpreter, started by run_in_fresh_process",
)
SERVICE_NAME = "billing"
ENVIRONMENT = "staging"
RELEASE = "1.2.3"
SECRET = "s3cr3t-value"


class Service(TypedDict):
    """What every test service passes to a builder, whatever the format."""

    service_name: str
    environment: str
    release: str


SERVICE = Service(service_name=SERVICE_NAME, environment=ENVIRONMENT, release=RELEASE)


class DomainError(Exception):
    """A failure the service maps to a 500."""


class MissingError(Exception):
    """A failure the service maps to a 404."""


def _monitoring(request: Request) -> MonitoringContract:
    return request.app.state.monitoring


def _build_app() -> FastAPI:
    """A service with the exception handler the lib documents.

    :func:`serve` attaches the monitoring and the engine the routes use.
    """
    app = FastAPI()
    statuses: dict[type[Exception], HTTPStatus] = {
        DomainError: HTTPStatus.INTERNAL_SERVER_ERROR,
        MissingError: HTTPStatus.NOT_FOUND,
    }

    async def handle(request: Request, exc: Exception) -> JSONResponse:
        status_code = statuses[type(exc)]
        _monitoring(request).record_server_error(exc=exc, status_code=status_code)
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    for error in statuses:
        app.add_exception_handler(exc_class_or_status_code=error, handler=handle)

    @app.get("/fail")
    def fail() -> None:
        raise DomainError("database down")

    @app.get("/missing")
    def missing() -> None:
        raise MissingError("no such user")

    @app.get("/degraded", status_code=HTTPStatus.CREATED)
    def degraded(request: Request) -> None:
        _monitoring(request).report(exc=DomainError("matching unavailable"))

    @app.get("/users/{user_id}")
    def user(user_id: int, request: Request) -> None:
        _monitoring(request).set_context(user_id=user_id, organization_id=7)
        _token = SECRET
        raise DomainError("user lookup failed")

    @app.get("/call")
    def call(url: str) -> dict[str, str]:
        return {"body": httpx.get(url).text}

    @app.get("/geocode")
    def geocode(url: str) -> None:
        httpx.get(url)
        raise DomainError("address not found")

    @app.get("/query")
    def query(request: Request) -> dict[str, int]:
        with request.app.state.engine.connect() as connection:
            return {"value": connection.execute(text("SELECT 1")).scalar_one()}

    return app


def serve(monitoring: MonitoringContract, engine: Engine | None = None) -> TestClient:
    """Instrument the test service with this monitoring, and return its client.

    The engine, if given, serves ``/query`` and is not instrumented here.
    """
    app = _build_app()
    monitoring.instrument_app(app=app)
    app.state.monitoring = monitoring
    app.state.engine = engine
    return TestClient(app=app, raise_server_exceptions=False)


@pytest.fixture
def downstream(httpserver_ipv4: HTTPServer) -> Callable[[str], str]:
    """Build the URL of a downstream service, on another port than the tool's.

    The Sentry SDK adds no trace header to a request going to its own DSN host.
    """

    def url(path: str) -> str:
        httpserver_ipv4.expect_request(path).respond_with_data("ok")
        return httpserver_ipv4.url_for(path)

    return url


def body_of(request: werkzeug.Request) -> bytes:
    """Read what a monitoring tool received, decompressed."""
    # The Sentry SDK sends brotli instead whenever the brotli module is importable.
    data = request.get_data()
    if request.headers.get("Content-Encoding") == "gzip":
        return gzip.decompress(data)
    return data


def run_in_fresh_process(
    *tests: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run tests in a new interpreter, for SDK settings read once per process.

    Returns:
        The finished pytest run, its summary in ``stdout``.
    """
    return subprocess.run(  # noqa: S603
        args=[
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
        ],
        env=os.environ | {FRESH_PROCESS: "1"} | dict(env or {}),
        capture_output=True,
        text=True,
        check=False,
    )

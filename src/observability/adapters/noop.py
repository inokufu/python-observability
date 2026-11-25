from collections.abc import Generator
from contextlib import contextmanager
from typing import override

from observability.domain.contract import ContextValue, MonitoringContract


class NoopMonitoring(MonitoringContract):
    """Monitoring without a destination: every call does nothing."""

    @override
    def report(self, exc: BaseException) -> None:
        pass

    @override
    def set_context(self, **attributes: ContextValue) -> None:
        pass

    @override
    @contextmanager
    def monitored_task(
        self,
        name: str,
        **attributes: ContextValue,
    ) -> Generator[None]:
        # Shaped like the real adapters', so dev and production accept the same uses.
        yield

    @override
    def instrument_app(self, app: object) -> None:
        pass

    @override
    def instrument_engine(self, engine: object) -> None:
        pass

    @override
    def shutdown(self) -> None:
        pass

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from http import HTTPStatus

type ContextValue = str | int | float | bool


class MonitoringContract(ABC):
    """What a service reports to its monitoring tool, whatever the format."""

    def record_server_error(self, exc: BaseException, status_code: int) -> None:
        """Report an error a handler turned into a response.

        Only a 5xx is reported: a 4xx is the client's error, not the service's. Call it
        inside a request or a task.

        Args:
            exc: The exception the handler caught.
            status_code: The HTTP status the handler answers with.
        """
        if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self.report(exc=exc)

    @abstractmethod
    def report(self, exc: BaseException) -> None:
        """Report an exception the service caught, whatever it answers.

        Call it inside a request or a task.

        Args:
            exc: The exception caught.
        """
        raise NotImplementedError

    @abstractmethod
    def set_context(self, **attributes: ContextValue) -> None:
        """Attach context, by name, to the current request or task.

        Only technical identifiers belong here, never a name or an email.
        """
        raise NotImplementedError

    @abstractmethod
    def monitored_task(
        self,
        name: str,
        **attributes: ContextValue,
    ) -> AbstractContextManager[None]:
        """Measure a task outside HTTP, and report then raise the exception ending it.

        Args:
            name: The task name.
            **attributes: The task context, as in :meth:`set_context`.
        """
        raise NotImplementedError

    @abstractmethod
    def instrument_app(self, app: object) -> None:
        """Measure the requests a web app serves.

        Call it after creating the app and before serving it.

        Args:
            app: The app.
        """
        raise NotImplementedError

    @abstractmethod
    def instrument_engine(self, engine: object) -> None:
        """Measure the queries a database engine runs.

        Call it once, right after creating the engine.

        Args:
            engine: The database engine to measure.
        """
        raise NotImplementedError

    @abstractmethod
    def shutdown(self) -> None:
        """Send what is pending and stop monitoring."""
        raise NotImplementedError

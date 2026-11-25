class ObservabilityError(Exception):
    """Base class for package exceptions."""


class InvalidPropagationTargetError(ObservabilityError):
    """Raised when a propagation target is not a valid regular expression."""

    def __init__(self) -> None:
        """Raise the error with its message."""
        super().__init__("A propagation target is not a valid regular expression")

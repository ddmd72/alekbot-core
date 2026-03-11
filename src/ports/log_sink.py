"""
Log Sink Port
=============

Defines an interface for emitting structured logs.
"""
from typing import Protocol, Dict, Any


class LogSink(Protocol):
    """Port for structured log emission."""

    def log(self, entry: Dict[str, Any]) -> None:
        """Emit a structured log entry."""
        ...
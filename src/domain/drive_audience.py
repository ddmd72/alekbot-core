"""Who is reading the drive right now (DRIVE_MCP_CONNECTOR_RFC §7.2).

A process-local ContextVar, the `run_deadline.CURRENT_RUN_DEADLINE` precedent. It is set by the drive MCP handler around
each tool call (and, in slice 4, by the link route around each download); unset means chat, whose reads are unchanged.
A task created inside the call inherits it (a task copies the context when it is created). It never rides in a message
context or a Cloud Task payload. The choke points read it (`UserDriveService.check_access`, `DriveSearchService`'s
index admit, `DriveConnectionService`'s connection reads); the access policy takes it as an argument and stays pure.

Public interface:
    DriveAudience
    CURRENT_DRIVE_AUDIENCE: ContextVar[Optional[DriveAudience]]
    connector_audience() -> context manager
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Iterator, Optional


class DriveAudience(str, Enum):
    CONNECTOR = "connector"


CURRENT_DRIVE_AUDIENCE: ContextVar[Optional[DriveAudience]] = ContextVar("current_drive_audience", default=None)


@contextmanager
def connector_audience() -> Iterator[None]:
    """Run the block under the connector audience; the previous value comes back on exit, an exception included."""
    token = CURRENT_DRIVE_AUDIENCE.set(DriveAudience.CONNECTOR)
    try:
        yield
    finally:
        CURRENT_DRIVE_AUDIENCE.reset(token)

"""One drive connector tool call's answer before composing (DRIVE_MCP_CONNECTOR_RFC §8): the outcome and, when the
call issued a one-time download link, its URL (a bearer secret: printed in the result, never logged)."""
from dataclasses import dataclass, field
from typing import Optional

from .drive_outcome import DriveOutcome


@dataclass(frozen=True)
class ConnectorCall:
    outcome: DriveOutcome
    link: Optional[str] = field(default=None, repr=False)

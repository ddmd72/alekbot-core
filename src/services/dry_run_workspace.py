"""Workspace stand-in for dry-run harnesses: reads are real, every mutation is answered without being executed."""
from typing import TYPE_CHECKING, Any, Mapping

from src.domain.workspace import WorkspaceCall, WorkspaceResult

if TYPE_CHECKING:
    from src.services.workspace_service import WorkspaceService

_READ_COMMANDS = frozenset({"view", "history"})
DRY_RUN_ACCEPTED = "(dry run) accepted; nothing was written"


class DryRunWorkspace:
    """Wraps the real WorkspaceService: `view` and `history` pass through, anything else is accepted and dropped."""

    def __init__(self, real: "WorkspaceService") -> None:
        self._real = real

    async def run(self, call: WorkspaceCall, command: str, args: Mapping[str, Any]) -> WorkspaceResult:
        if command in _READ_COMMANDS:
            return await self._real.run(call, command, args)
        return WorkspaceResult(text=DRY_RUN_ACCEPTED)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

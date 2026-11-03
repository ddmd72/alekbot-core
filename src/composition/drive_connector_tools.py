"""What the drive connector's tools run on (DRIVE_MCP_CONNECTOR_RFC §4.2, §4.4, §5.3, §11 slices 2 and 3).

`build_drive_connector_tools` is awaited by `main()` before `build_drive_mcp_app`. The tools are always built: what
the connector may read is each drive's connector access (§7; default nothing), checked under the connector audience
the drive MCP handler sets around each call.

- ONE connector `DriveOperations` from the graph's singletons (conversion, drive, search, notes), as
  `UserAgentFactory._drive_operations` builds the chat's, with no notifier, no localization and no language service, so
  no branch can post to a chat or send a receipt; wrapped in `DriveReadOperations` (five reads, nothing else).
- The texts are loaded once here: a failed first load raises (`ConnectorTextsUnavailable`; `ConnectorTextService`
  posts the ops alert through `alert_sink`), so the drive app is not built and `/drive-mcp` answers 404 (§5.3). A
  deployment without file conversion or without the prompt token store raises too: no app, never a half-built one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..domain.connector_texts import ConnectorTexts
from ..domain.drive_connector_contract import REQUIRED_RESULT_KEYS, REQUIRED_TOOL_KEYS
from ..infrastructure.drive_note_tools import ConnectorItemReader
from ..infrastructure.drive_operations import DriveOperations
from ..infrastructure.drive_read_operations import DriveReadOperations
from ..ports.alert_sink import AlertSinkPort
from ..ports.prompt_v3.token_repository import TokenRepository
from ..services.connector_text_service import ConnectorTextService
from ..services.drive_note_service import DriveNoteService
from ..services.drive_search_service import DriveSearchService
from ..services.file_conversion_service import FileConversionService
from ..services.user_drive_service import UserDriveService
from ..utils.logger import logger

TOOL_TEXTS_TOKEN = "MCP_DRIVE_TOOL_TEXTS"
RESULT_TEXTS_TOKEN = "MCP_DRIVE_RESULT_TEXTS"


@dataclass(frozen=True)
class DriveConnectorTools:
    """The connector's tool backends: the texts (with the boot-loaded value), the read facade, the note reader and the
    item reads `get_note` checks access with (`get_note` is registered only when both are present)."""
    texts: ConnectorTextService
    initial_texts: ConnectorTexts
    operations: DriveReadOperations
    notes: Optional[DriveNoteService]
    items: Optional[ConnectorItemReader]


async def build_drive_connector_tools(*, conversion_service: Optional[FileConversionService],
                                      drive_service: Optional[UserDriveService],
                                      drive_search: Optional[DriveSearchService],
                                      drive_notes: Optional[DriveNoteService],
                                      token_repo: Optional[TokenRepository],
                                      alert_sink: Optional[AlertSinkPort]) -> DriveConnectorTools:
    """Raises when the tools cannot run (see the module docstring)."""
    if conversion_service is None:
        raise RuntimeError("drive connector tools need file conversion, which this deployment does not have")
    if token_repo is None:
        raise RuntimeError("drive connector tools need the prompt token store, which did not initialize")
    texts = ConnectorTextService(token_repo, tool_token_id=TOOL_TEXTS_TOKEN, result_token_id=RESULT_TEXTS_TOKEN,
                                 required_tool_keys=REQUIRED_TOOL_KEYS, required_result_keys=REQUIRED_RESULT_KEYS,
                                 alert_sink=alert_sink)
    initial = await texts.texts()
    operations = DriveOperations(conversion_service=conversion_service, drive_service=drive_service,
                                 drive_search=drive_search, notification=None, localization=None,
                                 language_service=None, drive_notes=drive_notes)
    logger.info("[DriveMCP] tools built: texts loaded, notes %s",
                "on" if drive_notes is not None and drive_service is not None else "off")
    return DriveConnectorTools(texts=texts, initial_texts=initial, operations=DriveReadOperations(operations),
                               notes=drive_notes, items=drive_service)

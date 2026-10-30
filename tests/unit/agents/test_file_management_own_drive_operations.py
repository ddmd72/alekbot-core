"""FileManagementAgent built without a shared DriveOperations (every direct construction outside the factory) builds its
own from the services it was given (DRIVE_SPECIALIST_RFC §5.5). After the cut its own drive path is only the lenient
`open_file`, so each service is observed through what its instance does."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig
from src.domain.drive_search_line import SearchLine
from src.domain.language import LanguageCode
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

CTX = {"user_id": "u1", "account_id": "a1"}


async def test_its_own_drive_operations_holds_every_service_it_was_given():
    drive = AsyncMock(spec=UserDriveService)
    drive.status.side_effect = RuntimeError("offline")
    drive.delete.return_value = SimpleNamespace(item=SimpleNamespace(is_folder=False, path="Inbox/a.txt"))
    search = AsyncMock(spec=DriveSearchService)
    search.search_state.return_value = SearchLine.REBUILDING
    localization = MagicMock()
    localization.get_ui_string.return_value = "Deleted {path}"
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.EN
    notifier = AsyncMock()
    agent = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management", capabilities={},
                           metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        notification=notifier, drive_service=drive, localization=localization, language_service=language,
        drive_search=search)
    ops = agent.drive_operations
    assert (await ops.preflight("u1")).search is SearchLine.REBUILDING            # drive service + drive search
    drive.status.assert_awaited_once_with("u1")
    await ops.delete(CTX, "u1", "drive:x", wait_s=5)                              # notifier, localization, language
    language.resolve_ui_language.assert_awaited_once_with("u1")
    assert notifier.notify_raw.await_args.args[:3] == ("u1", "a1", "Deleted Inbox/a.txt")

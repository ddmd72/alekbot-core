"""Step 1 drive intents under the Step 2 error classes (USER_DRIVE_INDEXING_RFC §4).

A rejected refresh still reads as "not connected" (DriveReauthRequired is a DriveNotConnectedError).
A 401 after a successful refresh and exhausted throttling are operation failures, never a request to
reconnect a grant that is alive. The drive operations are DriveOperations' since the cut (DRIVE_SPECIALIST_RFC
§5.5): a reconnect is outcome class `not_connected`, an operation error `failed`, with today's texts."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_outcome_class import OutcomeClass
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveAccessDenied, DriveReauthRequired, DriveThrottled
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

_RECONNECT = "connect it in the Cabinet"


CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}


@pytest.fixture
def drive():
    return AsyncMock(spec=UserDriveService)


@pytest.fixture
def agent(drive):
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.EN
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), notification=AsyncMock(),
                           drive_service=drive, localization=MagicMock(), language_service=language)


async def test_rejected_refresh_still_asks_to_reconnect(agent, drive):
    drive.list_folder.side_effect = DriveReauthRequired("invalid_grant")
    resp = await agent.list_folder(CTX, "u1")
    assert not resp.success and _RECONNECT in resp.text and resp.cls is OutcomeClass.NOT_CONNECTED


@pytest.mark.parametrize("exc", [DriveAccessDenied("refused"), DriveThrottled(120.0)])
async def test_operation_errors_never_ask_to_reconnect(agent, drive, exc):
    drive.list_folder.side_effect = exc
    resp = await agent.list_folder(CTX, "u1")
    assert not resp.success and resp.cls is OutcomeClass.FAILED
    assert _RECONNECT not in resp.text and type(exc).__name__ in resp.text

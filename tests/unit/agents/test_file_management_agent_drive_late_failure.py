"""A delete/replace that fails after the agent answered "still running" tells the user (USER_DRIVE_RFC §4.13)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import DeleteOutcome, DriveItem, UpdateOutcome
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

FILE = DriveItem(item_id="f1", name="lease.pdf", path="Inbox/lease.pdf", is_folder=False,
                 size_bytes=2048, mime_type="application/pdf")

TEXTS = {
    UIMessage.DRIVE_DELETED_FILE: "DEL {path}",
    UIMessage.DRIVE_DELETED_FOLDER: "DELDIR {path} {count}",
    UIMessage.DRIVE_REPLACED: "REP {path} {before} {after}",
    UIMessage.DRIVE_DELETE_FAILED: "DELFAIL {path}",
    UIMessage.DRIVE_REPLACE_FAILED: "REPFAIL {path}",
}


def _msg(intent, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": intent, **payload}
    msg.context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
    return msg


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDriveService)
    d.get_item.return_value = FILE
    return d


@pytest.fixture
def notifier():
    return AsyncMock()


@pytest.fixture
def language():
    lang = AsyncMock()
    lang.resolve_ui_language.return_value = LanguageCode.FR
    return lang


@pytest.fixture
def agent(drive, notifier, language):
    localization = MagicMock()
    localization.get_ui_string.side_effect = lambda lang, m: TEXTS[m]
    a = FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
        notification=notifier, drive_service=drive, localization=localization, language_service=language,
    )
    a.MUTATION_WAIT_S = 0.05
    return a


async def _settle(agent):
    while agent._shielded or agent._notices:
        await asyncio.sleep(0.01)


def _late(exc=None, delay=0.15):
    async def run(*a, **k):
        await asyncio.sleep(delay)
        if exc:
            raise exc
        return DeleteOutcome(item=FILE, file_count=0)
    return run


async def test_late_failing_delete_posts_one_notice(agent, drive, notifier):
    drive.delete.side_effect = _late(RuntimeError("boom"))
    resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    assert agent.STILL_RUNNING in resp.result
    notifier.notify_raw.assert_not_called()
    await _settle(agent)
    notifier.notify_raw.assert_awaited_once()
    args, kw = notifier.notify_raw.await_args
    assert args[:3] == ("u1", "a1", "DELFAIL Inbox/lease.pdf")
    assert kw["channel_id_override"] == "C1" and kw["platform_override"] == "slack"


async def test_late_failing_replace_posts_notice(agent, drive, notifier):
    agent._source_bytes = AsyncMock(return_value=b"x")
    drive.replace_with.side_effect = _late(RuntimeError("boom"))
    resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", source_ref="new.pdf"))
    assert agent.STILL_RUNNING in resp.result
    await _settle(agent)
    notifier.notify_raw.assert_awaited_once()
    assert notifier.notify_raw.await_args.args[2] == "REPFAIL Inbox/lease.pdf"


async def test_late_success_posts_receipt_only(agent, drive, notifier):
    drive.delete.side_effect = _late()
    await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    await _settle(agent)
    notifier.notify_raw.assert_awaited_once()
    assert notifier.notify_raw.await_args.args[2] == "DEL Inbox/lease.pdf"


async def test_failure_while_still_waited_posts_no_notice(agent, drive, notifier):
    drive.delete.side_effect = _late(RuntimeError("boom"), delay=0.0)
    resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    assert resp.error
    await _settle(agent)
    notifier.notify_raw.assert_not_called()


async def test_other_intents_get_no_notice(agent, drive, notifier):
    async def slow(*a, **k):
        await asyncio.sleep(0.15)
        raise RuntimeError("boom")
    drive.append_text.side_effect = slow
    resp = await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", append_text="t"))
    assert agent.STILL_RUNNING in resp.result
    await _settle(agent)
    notifier.notify_raw.assert_not_called()


async def test_notify_failure_is_logged_not_raised(agent, drive, notifier):
    notifier.notify_raw.side_effect = RuntimeError("slack down")
    drive.delete.side_effect = _late(RuntimeError("boom"))
    await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    await _settle(agent)
    notifier.notify_raw.assert_awaited_once()


async def test_notice_uses_user_ui_language(agent, drive, language):
    drive.delete.side_effect = _late(RuntimeError("boom"))
    agent._localization.get_ui_string.side_effect = None
    agent._localization.get_ui_string.return_value = "DELFAIL {path}"
    await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    await _settle(agent)
    agent._localization.get_ui_string.assert_called_once_with(LanguageCode.FR, UIMessage.DRIVE_DELETE_FAILED)


async def test_replace_notice_names_the_path_captured_before_the_mutation(agent, drive, notifier):
    """The path is recorded from the pre-mutation lookup: no provider call after the failure
    (the provider is likely the thing that is down)."""
    agent._source_bytes = AsyncMock(return_value=b"x")
    drive.replace_with.side_effect = _late(RuntimeError("boom"))
    await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", source_ref="new.pdf"))
    drive.get_item.reset_mock()
    await _settle(agent)
    assert notifier.notify_raw.await_args.args[2] == "REPFAIL Inbox/lease.pdf"
    drive.get_item.assert_not_called()


async def test_delete_notice_falls_back_to_the_ref_when_the_lookup_fails(agent, drive, notifier):
    drive.delete.side_effect = _late(RuntimeError("boom"))
    drive.get_item.side_effect = RuntimeError("provider down")
    await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    await _settle(agent)
    notifier.notify_raw.assert_awaited_once()
    assert notifier.notify_raw.await_args.args[2] == "DELFAIL drive:f1"


@pytest.mark.parametrize("exc", [None, RuntimeError("boom")])
async def test_per_task_state_is_released(agent, drive, exc):
    agent._source_bytes = AsyncMock(return_value=b"x")
    drive.replace_with.side_effect = _late(exc)
    await agent.execute(_msg(Intent.UPDATE_FILE_IN_DRIVE, file_ref="drive:f1", source_ref="new.pdf"))
    await _settle(agent)
    assert not agent._armed and not agent._targets and not agent._shielded and not agent._notices


async def test_fast_failure_leaves_no_per_task_state(agent, drive):
    drive.delete.side_effect = _late(RuntimeError("boom"), delay=0.0)
    await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    await _settle(agent)
    assert not agent._armed and not agent._targets

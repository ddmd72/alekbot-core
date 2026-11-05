"""Every caller of `FileConversionService.resolve_bytes` turns `EmailFileNotTransferable` into its note as the
user-facing refusal (plan U4a A4 step 4): not a generic error, not "re-upload"."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.image_generation_agent import ImageGenerationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.drive_op import DriveOp
from src.domain.email_open_texts import EMAIL_FILE_NOT_TRANSFERABLE_NOTE as NOTE
from src.domain.email_ref import format_email_ref
from src.domain.exceptions import EmailFileNotTransferable, WorkspaceRefused
from src.infrastructure.drive_operations import DriveOperations, _Refusal
from src.services.workspace_service import WorkspaceService

REF = format_email_ref("msg_example001", "2")


def _conversion():
    conv = MagicMock()
    conv.resolve_bytes = AsyncMock(side_effect=EmailFileNotTransferable(NOTE))
    return conv


async def test_drive_source_bytes_refuses_with_the_note():
    ops = DriveOperations(_conversion())
    with pytest.raises(_Refusal) as caught:
        await ops._source_bytes(REF, "u1", DriveOp.COPY)
    assert caught.value.text == NOTE


async def test_workspace_copy_source_refuses_with_the_note():
    service = WorkspaceService(MagicMock(), file_conversion=_conversion())
    with pytest.raises(WorkspaceRefused) as caught:
        await service._read_source(MagicMock(user_id="u1"), REF)
    assert str(caught.value) == NOTE


async def test_image_edit_fails_with_the_note():
    agent = ImageGenerationAgent(
        config=AgentConfig(agent_id="img", agent_type="image_generation"),
        execution_context=MagicMock(), image_port=MagicMock(), prompt_builder=MagicMock(),
        file_conversion=_conversion())
    message = AgentMessage(intent=AgentIntent.QUERY, payload={}, sender="s", recipient="r", task_id="t",
                           context={"user_id": "u1"})
    response = await agent._execute_edit(message, "make it blue", [REF])
    assert response.status == AgentStatus.FAILED and response.error == NOTE

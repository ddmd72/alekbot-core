from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.domain.agent import AgentResponse, AgentStatus


@pytest.fixture
def context_builder():
    cb = MagicMock()
    cb.build.return_value = MagicMock(provider=AsyncMock(), model_name="claude-sonnet-5")
    return cb


@pytest.fixture
def user_repo():
    r = MagicMock()
    r.get_user = AsyncMock(return_value=MagicMock(config=MagicMock()))
    return r


@pytest.fixture
def runner(context_builder, user_repo):
    return CompanionExtractorRunner(
        context_builder=context_builder, user_repo=user_repo, prompt_builder=AsyncMock(),
    )


async def test_unknown_companion_type_raises(runner):
    with pytest.raises(ValueError, match="No extractor registered"):
        await runner.extract(
            companion_type="moderator", account_id="acc-1",
            created_by_user_id="user-1", messages=[],
        )


async def test_unknown_user_raises(runner, user_repo):
    user_repo.get_user.return_value = None
    with pytest.raises(ValueError, match="not found"):
        await runner.extract(
            companion_type="tutor", account_id="acc-1",
            created_by_user_id="user-1", messages=[],
        )


async def test_extract_success_returns_agent_result(runner, monkeypatch):
    fake_response = AgentResponse.success(
        task_id="t1", agent_id="tutor_extractor_user-1",
        result={"records": [], "summary": "ok"},
    )
    mock_process = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(
        "src.composition.companion_extractor_runner.TutorExtractorAgent.process",
        mock_process,
    )

    result = await runner.extract(
        companion_type="tutor", account_id="acc-1",
        created_by_user_id="user-1", messages=[{"role": "user", "parts": [{"text": "hi"}]}],
    )

    assert result == {"records": [], "summary": "ok"}
    mock_process.assert_called_once()


async def test_extract_agent_failure_raises(runner, monkeypatch):
    fake_response = AgentResponse.failure(task_id="t1", agent_id="tutor_extractor_user-1", error="boom")
    monkeypatch.setattr(
        "src.composition.companion_extractor_runner.TutorExtractorAgent.process",
        AsyncMock(return_value=fake_response),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await runner.extract(
            companion_type="tutor", account_id="acc-1",
            created_by_user_id="user-1", messages=[{"role": "user", "parts": [{"text": "hi"}]}],
        )

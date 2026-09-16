from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.domain.agent import AgentResponse, AgentStatus
from src.ports.prompt_builder_port import PromptBuilderPort


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


# ---------------------------------------------------------------------------
# Regression coverage for the final-review fix wave (2026-08-27):
#   1. main.py was passing container.assembly_service (a PromptAssemblyService,
#      no build_for_agent) as prompt_builder instead of a real PromptBuilder
#      wrapper — every companion extraction failed at runtime with an
#      AttributeError swallowed by the agent's own try/except.
#   2. CompanionExtractorRunner never wired _quota_service / _prompt_content_store
#      onto the agent it constructs (UserAgentFactory does this for every other
#      agent) — companion extraction's LLM usage was invisible to billing and
#      BigQuery content capture.
# These tests are additive only — none of the fixtures/tests above are touched.
# ---------------------------------------------------------------------------

def test_prompt_builder_wrapping_assembly_service_satisfies_port():
    """
    Proves the object main.py now constructs (PromptBuilder(repo=None,
    assembly_service=...)) actually satisfies PromptBuilderPort — in
    particular that it has build_for_agent, which container.assembly_service
    (a bare PromptAssemblyService) does NOT have. This is the fix for
    Required fix 1: main.py must wrap the assembly service, not pass it raw.
    """
    from src.services.prompt_builder import PromptBuilder

    wrapped = PromptBuilder(repo=None, assembly_service=MagicMock())

    assert isinstance(wrapped, PromptBuilderPort)
    assert hasattr(wrapped, "build_for_agent")
    assert callable(wrapped.build_for_agent)


def test_constructor_stores_prompt_builder_port_shaped_object():
    """
    Uses spec=PromptBuilderPort (not a bare AsyncMock()) deliberately: a bare
    AsyncMock() answers any attribute access, which is exactly what let
    main.py hand CompanionExtractorRunner a PromptAssemblyService (missing
    build_for_agent) without any test noticing. A spec'd mock only exposes
    PromptBuilderPort's real methods, and isinstance() against it succeeds
    only because it matches that spec.
    """
    context_builder = MagicMock()
    user_repo = MagicMock()
    prompt_builder = MagicMock(spec=PromptBuilderPort)

    runner = CompanionExtractorRunner(
        context_builder=context_builder, user_repo=user_repo, prompt_builder=prompt_builder,
    )

    assert isinstance(runner._prompt_builder, PromptBuilderPort)


async def test_extract_wires_quota_service_and_prompt_content_store_onto_agent(monkeypatch):
    """
    Required fix 2: CompanionExtractorRunner bypasses UserAgentFactory by
    design, so nothing else performs the post-construction wiring that makes
    _flush_billing()/BigQuery content capture work (both silently no-op when
    the corresponding attribute is None, which is BaseAgent's default).
    Captures the actual constructed agent instance via a plain function
    (not a Mock) substituted for TutorExtractorAgent.process, so the
    descriptor protocol binds `self` the normal way.
    """
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=AsyncMock(), model_name="claude-sonnet-5")
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=MagicMock(config=MagicMock()))
    quota_service = MagicMock()
    prompt_content_store = MagicMock()

    runner = CompanionExtractorRunner(
        context_builder=context_builder,
        user_repo=user_repo,
        prompt_builder=AsyncMock(),
        quota_service=quota_service,
        prompt_content_store=prompt_content_store,
    )

    captured = {}

    async def fake_process(self, message):
        captured["agent"] = self
        return AgentResponse.success(
            task_id="t1", agent_id=self.agent_id, result={"records": [], "summary": "ok"},
        )

    monkeypatch.setattr(
        "src.composition.companion_extractor_runner.TutorExtractorAgent.process",
        fake_process,
    )

    await runner.extract(
        companion_type="tutor", account_id="acc-1",
        created_by_user_id="user-1", messages=[{"role": "user", "parts": [{"text": "hi"}]}],
    )

    agent = captured["agent"]
    assert agent._quota_service is quota_service
    assert agent._prompt_content_store is prompt_content_store


async def test_extract_defaults_quota_service_and_prompt_content_store_to_none(runner, monkeypatch):
    """The default-constructed `runner` fixture passes neither kwarg — confirms
    the new params are optional and default to None (BaseAgent's own default),
    same as before this fix wave for callers that don't pass them."""
    captured = {}

    async def fake_process(self, message):
        captured["agent"] = self
        return AgentResponse.success(
            task_id="t1", agent_id=self.agent_id, result={"records": [], "summary": "ok"},
        )

    monkeypatch.setattr(
        "src.composition.companion_extractor_runner.TutorExtractorAgent.process",
        fake_process,
    )

    await runner.extract(
        companion_type="tutor", account_id="acc-1",
        created_by_user_id="user-1", messages=[{"role": "user", "parts": [{"text": "hi"}]}],
    )

    agent = captured["agent"]
    assert agent._quota_service is None
    assert agent._prompt_content_store is None

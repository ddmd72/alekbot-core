from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.tutor_agent import TutorAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.companion_context import CompanionContext
from src.domain.companion import CompanionRecord
from src.infrastructure.agent_config import TUTOR
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort


@pytest.fixture
def mock_llm():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text="¡Hola! Let's practice the subjunctive today.",
        usage_metadata=MagicMock(total_tokens=42),
        tool_calls=[],
    )
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are the Spanish tutor."
    return pb


@pytest.fixture
def mock_assembler():
    svc = AsyncMock()
    svc.assemble_context.return_value = CompanionContext(
        session_summary="Covered subjunctive mood last time.",
        own_records=[
            CompanionRecord(
                session_id="slack:C1", account_id="acc-1", created_by_user_id="user-1",
                text="Confuses subjunctive after 'ojalá'", domain="grammar_error",
            ),
        ],
        biographical_facts=[],
        standing_directives=[],
    )
    return svc


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_assembler):
    config = AgentConfig(agent_id="tutor_agent_test", agent_type="tutor",
                         timeout_ms=60000, capabilities=["tutor_chat"])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    return TutorAgent(
        config=config, execution_context=ctx, prompt_builder=mock_prompt_builder,
        assembler=mock_assembler, user_id=None,
    )


def _bound_message(query="¿Cómo se dice 'I would like'?", history=None):
    return AgentMessage(
        task_id="t1", sender="coordinator", recipient="tutor_agent_test",
        intent=AgentIntent.QUERY,
        payload={"query": query, "intent": "tutor_chat"},
        context={
            "account_id": "acc-1", "user_id": "user-1",
            "origin_channel_id": "C1", "origin_platform": "slack",
            "history": history or [],
        },
    )


async def test_can_handle_correct(agent):
    assert await agent.can_handle(_bound_message()) is True


async def test_can_handle_wrong_intent():
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    agent = TutorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock(), assembler=AsyncMock())
    msg = _bound_message()
    msg.intent = AgentIntent.INFORM
    assert await agent.can_handle(msg) is False


async def test_can_handle_empty_query():
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    agent = TutorAgent(config=config, execution_context=MagicMock(), prompt_builder=AsyncMock(), assembler=AsyncMock())
    assert await agent.can_handle(_bound_message(query="")) is False


async def test_execute_happy_path(agent, mock_prompt_builder, mock_assembler):
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    assert "subjunctive" in response.result.text.lower()


async def test_execute_calls_assembler_with_platform_qualified_session_id(agent, mock_assembler):
    await agent.execute(_bound_message())
    mock_assembler.assemble_context.assert_called_once()
    call_kwargs = mock_assembler.assemble_context.call_args.kwargs
    assert call_kwargs["session_id"] == "slack:C1"
    assert call_kwargs["account_id"] == "acc-1"
    assert call_kwargs["include_biographical"] is False
    assert call_kwargs["include_standing_directives"] is False
    assert call_kwargs["user_id"] is None


async def test_execute_injects_companion_context_as_static_block(agent, mock_prompt_builder):
    await agent.execute(_bound_message())
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    blocks = call_kwargs.get("extra_static_blocks") or []
    joined = "\n".join(blocks)
    assert "companion_context" in joined
    assert "subjunctive" in joined


async def test_execute_strips_delegation_timestamp_from_retrieval_query(agent, mock_assembler):
    # AgentCoordinator.handle_delegation() prepends "[Mon DD, HH:MM UTC] " to every
    # delegated query before it reaches a specialist (agent_coordinator.py:424-425).
    # The retrieval phrase embedded for own-records RRF search must be the clean
    # utterance, not that prefix — the timestamp is noise that changes every minute.
    prefixed_query = "[Aug 29, 14:32 UTC] ¿Cómo se dice 'I would like'?"
    await agent.execute(_bound_message(query=prefixed_query))
    call_kwargs = mock_assembler.assemble_context.call_args.kwargs
    assert call_kwargs["query_phrases"] == ["¿Cómo se dice 'I would like'?"]


async def test_execute_skips_empty_companion_context_block(agent, mock_assembler, mock_prompt_builder):
    mock_assembler.assemble_context.return_value = CompanionContext(
        session_summary=None, own_records=[], biographical_facts=[], standing_directives=[],
    )
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    assert not (call_kwargs.get("extra_static_blocks") or [])


async def test_execute_missing_origin_context_logs_warning_and_degrades(agent, caplog, mock_prompt_builder):
    import logging

    msg = _bound_message()
    del msg.context["origin_platform"]
    with caplog.at_level(logging.WARNING):
        response = await agent.execute(msg)
    assert response.status == AgentStatus.SUCCESS
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    assert not (call_kwargs.get("extra_static_blocks") or [])
    assert "companion context skipped" in " ".join(caplog.messages)


async def test_execute_passes_include_directives_false(agent, mock_prompt_builder):
    # RFC §5: the tutor's permission boundary should be explicit at the call
    # site, not an implicit side-effect of include_biographical=False.
    await agent.execute(_bound_message())
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    assert call_kwargs["include_directives"] is False


async def test_execute_reads_history_from_context_not_session_store(agent, mock_llm):
    # No SessionStore dependency exists on this agent at all — the constructor
    # never receives or stores one (see the `agent` fixture). This test verifies
    # the substantive behavior: history from message.context["history"] actually
    # reaches the LLM request, not just that some call happened.
    history = [{"role": "user", "parts": [{"text": "Hola profe"}]}]
    await agent.execute(_bound_message(history=history))
    request = mock_llm.generate_content.call_args.kwargs["request"]
    history_texts = [part.text for msg in request.messages for part in msg.parts if part.text]
    # Substring containment, not exact equality: _inject_timestamps only stamps a
    # "[Mon DD, HH:MM TZ] " prefix onto user turns that carry `created_at` (see
    # BaseAgent._inject_timestamps), and this history entry has none — so no prefix
    # is actually added here today. `in` keeps this test robust either way.
    assert any("Hola profe" in t for t in history_texts)


async def test_execute_assembler_failure_degrades_gracefully(agent, mock_assembler, mock_prompt_builder):
    mock_assembler.assemble_context.side_effect = Exception("Firestore down")
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    assert not (call_kwargs.get("extra_static_blocks") or [])


async def test_execute_no_query_fails(agent):
    response = await agent.execute(_bound_message(query=""))
    assert response.status == AgentStatus.FAILED


async def test_execute_prompt_builder_failure(mock_llm, mock_assembler):
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.side_effect = Exception("prompt missing")
    config = AgentConfig(agent_id="a", agent_type="tutor", timeout_ms=1000, capabilities=[])
    ctx = MagicMock(provider=mock_llm, model_name="claude-sonnet-5")
    agent = TutorAgent(config=config, execution_context=ctx, prompt_builder=pb, assembler=mock_assembler)
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.FAILED
    assert "PromptBuilder failed" in response.error


async def test_execute_llm_exception(agent, mock_llm):
    mock_llm.generate_content.side_effect = Exception("LLM error")
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.FAILED


def test_get_alternative_agents_empty(agent):
    assert agent._get_alternative_agents() == []


# ---------------------------------------------------------------------------
# DelegationEngine branch (fix wave 2026-08-29, Required fix 4)
#
# Every test above leaves agent.coordinator unset (None, BaseAgent's default),
# which routes execute() through the single-LLM-call fallback branch in
# _converse(). In production, UserAgentFactory.create_agent_on_demand ALWAYS
# sets agent.coordinator = self.coordinator, and search_web (the tutor's only
# allowed intent — search_memory was dropped per fix 3) is always available as
# a tool, so production always takes the DelegationEngine branch instead. These
# two tests are the only coverage of that branch.
# ---------------------------------------------------------------------------

def _coordinator_with_search_web():
    coordinator = MagicMock()
    coordinator.get_available_intents_for.return_value = [
        {"name": "search_web", "description": "Search the web.", "context_schema": {}},
    ]
    return coordinator


async def test_execute_uses_delegation_engine_when_coordinator_available(agent):
    coordinator = _coordinator_with_search_web()
    agent.coordinator = coordinator

    engine_result = MagicMock(
        failed=False, terminal_tool_args=None, text="¡Vale! Delegated answer.", total_tokens=99
    )
    mock_engine = MagicMock()
    mock_engine.execute = AsyncMock(return_value=engine_result)

    with patch("src.agents.tutor_agent.DelegationEngine", return_value=mock_engine) as mock_engine_cls:
        response = await agent.execute(_bound_message())

    mock_engine_cls.assert_called_once_with(coordinator)
    mock_engine.execute.assert_awaited_once()
    call_kwargs = mock_engine.execute.call_args.kwargs
    assert call_kwargs["max_turns"] == TUTOR.max_delegation_turns
    assert call_kwargs["calling_agent_id"] == agent.agent_id
    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "¡Vale! Delegated answer."


async def test_execute_delegation_engine_max_turns_exhausted_fails(agent):
    coordinator = _coordinator_with_search_web()
    agent.coordinator = coordinator

    engine_result = MagicMock(failed=True, text="", total_tokens=0)
    mock_engine = MagicMock()
    mock_engine.execute = AsyncMock(return_value=engine_result)

    with patch("src.agents.tutor_agent.DelegationEngine", return_value=mock_engine):
        response = await agent.execute(_bound_message())

    assert response.status == AgentStatus.FAILED
    assert response.error == "max_turns_exhausted"


async def test_execute_preserves_full_text_for_recent_turns(agent, mock_llm):
    # BaseAgent._apply_history_tier's use_full check happens BEFORE model_turns_from_end
    # is incremented, so passing max_full_turns=5 (unadjusted, matching Smart's own call
    # exactly) actually keeps the 6 most recent model turns in full text, not 5 — this
    # off-by-one is baked into the shared method and is Smart's own real production
    # behavior too. 7 model turns here so turn 0 (the 7th-from-newest) genuinely falls
    # outside that 6-turn window and must be summary-only.
    history = []
    for i in range(7):
        history.append({"role": "user", "parts": [{"text": f"question {i}"}]})
        history.append({
            "role": "model",
            "parts": [{"text": f"summary {i}", "full_text": f"the full detailed answer {i}"}],
        })
    await agent.execute(_bound_message(history=history))
    request = mock_llm.generate_content.call_args.kwargs["request"]
    texts = [part.text for msg in request.messages for part in msg.parts if part.text]
    joined = " ".join(texts)
    # Oldest model turn (0) is the 7th-from-newest — outside the 6-turn full window.
    assert "summary 0" in joined
    assert "the full detailed answer 0" not in joined
    # Newest model turn (6) is within the window → full text, not summary.
    assert "the full detailed answer 6" in joined


# ---------------------------------------------------------------------------
# Structured-output contract (Phase G Task 4): TutorAgent now returns
# SmartResponse via extract_structured_response, mirroring SmartResponseAgent.
# ---------------------------------------------------------------------------


async def test_execute_returns_smart_response_with_full_response_field(agent, mock_llm):
    mock_llm.generate_content.return_value = MagicMock(
        text='{"full_response": "Vale, sigamos.", "response_summary": "Discussed subjunctive."}',
        usage_metadata=MagicMock(total_tokens=10), tool_calls=[],
    )
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Vale, sigamos."
    assert response.metadata["response_summary"] == "Discussed subjunctive."
    request = mock_llm.generate_content.call_args.kwargs["request"]
    assert request.response_schema == agent._RESPONSE_SCHEMA


async def test_execute_grok_terminal_tool_args_extracted(agent):
    coordinator = _coordinator_with_search_web()
    agent.coordinator = coordinator

    engine_result = MagicMock(
        failed=False,
        terminal_tool_args={
            "full_response": "Grok answer.",
            "response_summary": "Grok summary.",
        },
        text="",
        total_tokens=55,
    )
    mock_engine = MagicMock()
    mock_engine.execute = AsyncMock(return_value=engine_result)

    with patch("src.agents.tutor_agent.DelegationEngine", return_value=mock_engine):
        response = await agent.execute(_bound_message())

    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Grok answer."
    assert response.metadata["response_summary"] == "Grok summary."


async def test_execute_passes_terminal_tool_to_engine(agent):
    coordinator = _coordinator_with_search_web()
    agent.coordinator = coordinator

    engine_result = MagicMock(failed=False, terminal_tool_args=None, text="Answer.", total_tokens=1)
    mock_engine = MagicMock()
    mock_engine.execute = AsyncMock(return_value=engine_result)

    with patch("src.agents.tutor_agent.DelegationEngine", return_value=mock_engine):
        await agent.execute(_bound_message())

    call_kwargs = mock_engine.execute.call_args.kwargs
    assert call_kwargs["terminal_tool"] == "deliver_response"


# ---------------------------------------------------------------------------
# Phase G Task 5: async HistorySummaryService fallback when the LLM omits
# response_summary from the structured-output contract (Task 4).
# ---------------------------------------------------------------------------


async def test_execute_creates_async_summary_task_when_summary_missing(agent, mock_llm):
    summary_service = AsyncMock()
    summary_service.summarize_model_response = AsyncMock(return_value="Async summary.")
    agent.history_summary_service = summary_service

    mock_llm.generate_content.return_value = MagicMock(
        text='{"full_response": "Vale.", "response_summary": ""}',
        usage_metadata=MagicMock(total_tokens=5), tool_calls=[],
    )
    response = await agent.execute(_bound_message())

    assert response.status == AgentStatus.SUCCESS
    assert "response_summary_task" in response.metadata
    result = await response.metadata["response_summary_task"]
    assert result == "Async summary."
    summary_service.summarize_model_response.assert_called_once_with("Vale.")


async def test_execute_no_async_task_when_sync_summary_present(agent, mock_llm):
    summary_service = AsyncMock()
    agent.history_summary_service = summary_service

    mock_llm.generate_content.return_value = MagicMock(
        text='{"full_response": "Vale.", "response_summary": "Already have one."}',
        usage_metadata=MagicMock(total_tokens=5), tool_calls=[],
    )
    response = await agent.execute(_bound_message())

    assert "response_summary_task" not in response.metadata
    summary_service.summarize_model_response.assert_not_called()


async def test_execute_no_service_configured_no_crash(agent, mock_llm):
    # agent fixture never sets history_summary_service — defaults to None.
    mock_llm.generate_content.return_value = MagicMock(
        text='{"full_response": "Vale.", "response_summary": ""}',
        usage_metadata=MagicMock(total_tokens=5), tool_calls=[],
    )
    response = await agent.execute(_bound_message())
    assert response.status == AgentStatus.SUCCESS
    assert "response_summary_task" not in response.metadata


async def test_own_records_rendered_with_iso_date_prefix(agent, mock_assembler, mock_prompt_builder):
    from datetime import datetime, timezone
    mock_assembler.assemble_context.return_value = CompanionContext(
        session_summary="Covered subjunctive.",
        own_records=[
            CompanionRecord(
                session_id="slack:C1", account_id="acc-1", created_by_user_id="user-1",
                text="Confuses subjunctive after 'ojalá'", domain="grammar_error",
                created_at=datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc),
            ),
        ],
        biographical_facts=[], standing_directives=[],
    )
    await agent.execute(_bound_message())
    call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
    joined = "\n".join(call_kwargs.get("extra_static_blocks") or [])
    assert "[2026-08-15] Confuses subjunctive after 'ojalá'" in joined

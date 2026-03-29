"""
Unit tests for ConsolidationAgent and _TrackingFactManagement.

Mock boundary: LLMPort, FactRepository, FactWritePort, FactManagementPort, PromptBuilderPort.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.consolidation_agent import ConsolidationAgent, _TrackingFactManagement
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.fact_management_port import FactManagementPort
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMResponse,
    Message,
    MessagePart,
    ProviderCapabilities,
    ToolCall,
)

_USER_ID = "user-test-01"
_ACCOUNT_ID = "account-test-01"


def _make_agent(mock_llm=None, fact_management=None, prompt_builder=None, indexed_email_repo=None):
    config = AgentConfig(agent_id="consolidation_agent", agent_type="consolidation")
    mock_provider = MagicMock(spec=LLMPort)
    if mock_llm:
        mock_provider.generate_content = mock_llm
    else:
        mock_provider.generate_content = AsyncMock(return_value=LLMResponse(text="{}"))

    execution_context = AgentExecutionContext(
        agent_type="consolidation",
        provider=mock_provider,
        model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )
    repo = MagicMock()
    repo.get_biographical_context_cached = AsyncMock(return_value=[])
    repo.refresh_biographical_context_cache = AsyncMock()
    repo.get_active_facts = AsyncMock(return_value=[])
    repo.get_longest_facts = AsyncMock(return_value=[])
    repo.archive_observations = AsyncMock()

    embedding = MagicMock()
    fact_write = MagicMock()
    fact_write.add_facts_batch = AsyncMock(return_value=(0, 0, []))

    pb = prompt_builder or MagicMock()
    pb.build_for_agent = AsyncMock(return_value="SYSTEM PROMPT")
    pb.invalidate_biographical_cache = MagicMock()

    agent = ConsolidationAgent(
        config=config,
        execution_context=execution_context,
        repository=repo,
        embedding_service=embedding,
        fact_write_service=fact_write,
        fact_management_port=fact_management,
        prompt_builder=pb,
        indexed_email_repo=indexed_email_repo,
    )
    agent._llm = mock_provider
    return agent, repo, fact_write, pb


def _make_message(task: str, payload_extra: dict = None, context_extra: dict = None):
    payload = {"task": task}
    if payload_extra:
        payload.update(payload_extra)
    context = {"user_id": _USER_ID}
    if context_extra:
        context.update(context_extra)
    return AgentMessage.create(
        sender="test",
        recipient="consolidation_agent",
        intent=AgentIntent.DELEGATE,
        payload=payload,
        context=context,
    )


# ==============================================================================
# TestTrackingFactManagement
# ==============================================================================

class TestTrackingFactManagement:

    def _make_tracking(self):
        real = AsyncMock(spec=FactManagementPort)
        return _TrackingFactManagement(real), real

    async def test_search_passthrough(self):
        tracker, real = self._make_tracking()
        real.search_existing_facts = AsyncMock(return_value=[{"fact_id": "f1"}])
        result = await tracker.search_existing_facts(
            keywords=["kw"], primary_query="q", alternative_query="alt", limit=5
        )
        assert result == [{"fact_id": "f1"}]
        real.search_existing_facts.assert_awaited_once_with(
            keywords=["kw"], primary_query="q", alternative_query="alt", limit=5
        )
        assert tracker.changed == []

    async def test_create_fact_with_fact_id(self):
        tracker, real = self._make_tracking()
        real.create_fact = AsyncMock(return_value={"fact_id": "new-id", "status": "created"})
        result = await tracker.create_fact(content="some content", metadata={})
        assert result["fact_id"] == "new-id"
        assert tracker.changed == [("new-id", "some content")]

    async def test_create_fact_without_fact_id(self):
        tracker, real = self._make_tracking()
        real.create_fact = AsyncMock(return_value={"status": "error"})
        await tracker.create_fact(content="content", metadata={})
        assert tracker.changed == []

    async def test_update_fact_with_content(self):
        tracker, real = self._make_tracking()
        real.update_fact = AsyncMock(return_value={"status": "updated"})
        await tracker.update_fact(fact_id="fid-1", updates={"content": "updated text"})
        assert tracker.changed == [("fid-1", "updated text")]

    async def test_update_fact_without_content(self):
        tracker, real = self._make_tracking()
        real.update_fact = AsyncMock(return_value={"status": "updated"})
        await tracker.update_fact(fact_id="fid-1", updates={"state": "SUPERSEDED"})
        assert tracker.changed == []

    async def test_merge_facts_with_new_fact_id(self):
        tracker, real = self._make_tracking()
        real.merge_facts = AsyncMock(return_value={"new_fact_id": "merged-id"})
        await tracker.merge_facts(
            fact_ids=["a", "b"], merged_content="merged", metadata={}
        )
        assert tracker.changed == [("merged-id", "merged")]

    async def test_merge_facts_without_new_fact_id(self):
        tracker, real = self._make_tracking()
        real.merge_facts = AsyncMock(return_value={"status": "error"})
        await tracker.merge_facts(fact_ids=["a"], merged_content="m", metadata={})
        assert tracker.changed == []

    async def test_discard_candidate_passthrough(self):
        tracker, real = self._make_tracking()
        real.discard_candidate = AsyncMock(return_value={"status": "discarded"})
        result = await tracker.discard_candidate(reason="irrelevant")
        assert result == {"status": "discarded"}
        real.discard_candidate.assert_awaited_once_with(reason="irrelevant")
        assert tracker.changed == []


# ==============================================================================
# TestCanHandle
# ==============================================================================

class TestCanHandle:

    @pytest.mark.parametrize("task", [
        "consolidate",
        "consolidate_cluster",
        "consolidate_email",
        "consolidate_full",
        "synthesize",
        "librarian",
    ])
    async def test_valid_tasks(self, task):
        agent, _, _, _ = _make_agent()
        msg = _make_message(task)
        assert await agent.can_handle(msg) is True

    async def test_wrong_intent(self):
        agent, _, _, _ = _make_agent()
        msg = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.QUERY,
            payload={"task": "consolidate"},
            context={"user_id": _USER_ID},
        )
        assert await agent.can_handle(msg) is False

    async def test_unknown_task_string(self):
        agent, _, _, _ = _make_agent()
        msg = _make_message("unknown_task")
        assert await agent.can_handle(msg) is False


# ==============================================================================
# TestExecuteEarlyExits
# ==============================================================================

class TestExecuteEarlyExits:

    async def test_no_user_id_returns_failure(self):
        agent, _, _, _ = _make_agent()
        msg = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "consolidate"},
            context={},
        )
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
        assert "user_id" in response.error.lower()

    async def test_no_account_id_no_request_context_returns_failure(self):
        agent, _, _, _ = _make_agent()
        msg = _make_message("consolidate")
        # No RequestContext set → get_effective_account_id() returns None → ValueError caught
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED

    async def test_bio_context_exception_swallowed_continues(self):
        agent, repo, fact_write, _ = _make_agent()
        repo.get_biographical_context_cached = AsyncMock(side_effect=Exception("cache error"))

        # No messages or observations — should still reach the "No data" early return
        msg = _make_message("consolidate", payload_extra={"messages": [], "observations": []})
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["message"] == "No data"


# ==============================================================================
# TestExecuteDispatch
# ==============================================================================

class TestExecuteDispatch:

    async def test_dispatch_consolidate_cluster(self):
        agent, repo, _, pb = _make_agent()
        repo.get_longest_facts = AsyncMock(return_value=[])

        with patch.object(agent, "_handle_consolidate_cluster", new=AsyncMock()) as mock_handler:
            mock_handler.return_value = MagicMock(status=AgentStatus.SUCCESS, result={}, error=None)
            msg = _make_message("consolidate_cluster")
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                await agent.execute(msg)
            mock_handler.assert_awaited_once()

    async def test_dispatch_consolidate_email(self):
        agent, _, _, _ = _make_agent()
        with patch.object(agent, "_handle_consolidate_email", new=AsyncMock()) as mock_handler:
            mock_handler.return_value = MagicMock(status=AgentStatus.SUCCESS, result={}, error=None)
            msg = _make_message("consolidate_email")
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                await agent.execute(msg)
            mock_handler.assert_awaited_once()

    async def test_dispatch_consolidate_full(self):
        agent, _, _, _ = _make_agent()
        with patch.object(agent, "_handle_consolidate_full", new=AsyncMock()) as mock_handler:
            mock_handler.return_value = MagicMock(status=AgentStatus.SUCCESS, result={}, error=None)
            msg = _make_message("consolidate_full")
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                await agent.execute(msg)
            mock_handler.assert_awaited_once()

    async def test_unknown_task_returns_failure(self):
        agent, _, _, _ = _make_agent()
        msg = AgentMessage.create(
            sender="test",
            recipient="consolidation_agent",
            intent=AgentIntent.DELEGATE,
            payload={"task": "not_a_real_task"},
            context={"user_id": _USER_ID},
        )
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
        assert "Unknown task" in response.error

    async def test_exception_in_handler_returns_failure(self):
        agent, _, _, _ = _make_agent()
        with patch.object(agent, "_handle_consolidate", new=AsyncMock(side_effect=RuntimeError("boom"))):
            msg = _make_message("consolidate")
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
        assert "boom" in response.error


# ==============================================================================
# TestHandleConsolidate
# ==============================================================================

class TestHandleConsolidate:

    async def test_v3_path_calls_deliberate_process(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, _, _ = _make_agent(fact_management=fm)
        agent.prompt_version = "v3"

        ops = [{"action": "CREATE", "fact_id": "x"}]
        with patch.object(
            agent, "_execute_deliberate_process_v3",
            new=AsyncMock(return_value={"operations": ops})
        ):
            msg = _make_message("consolidate", payload_extra={"messages": [{"role": "user", "text": "hi"}]})
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["operations"] == 1
        assert response.metadata["version"] == "v3"

    async def test_v2_no_data_returns_success(self):
        agent, _, _, _ = _make_agent()
        agent.prompt_version = "v2"

        msg = _make_message("consolidate", payload_extra={"messages": [], "observations": []})
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["message"] == "No data"

    async def test_v2_observations_deprecated_path(self):
        agent, repo, fact_write, _ = _make_agent()
        agent.prompt_version = "v2"
        fact_write.add_facts_batch = AsyncMock(return_value=(0, 0, []))

        msg = _make_message(
            "consolidate",
            payload_extra={"messages": [], "observations": [{"id": "obs1"}]}
        )
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent.execute(msg)

        # deprecated path sets results = {"new_facts": [], "new_anchors": []}
        # → sanitized items are empty → observations branch logs error and returns FAILED
        assert response.status == AgentStatus.FAILED
        assert "invalid format" in response.error.lower()

    async def test_v2_with_messages_calls_synthesize(self):
        agent, repo, fact_write, _ = _make_agent()
        agent.prompt_version = "v2"

        synthesize_result = {"new_facts": [{"id": "f1", "text": "fact"}], "new_anchors": []}
        with patch.object(
            agent, "_synthesize_session_facts_v2",
            new=AsyncMock(return_value=synthesize_result)
        ):
            msg = _make_message(
                "consolidate",
                payload_extra={"messages": [{"role": "user", "text": "hello"}]}
            )
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["new_facts"] == 1
        assert response.metadata["version"] == "v2"

    async def test_v3_results_triggers_cache_refresh(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, _, pb = _make_agent(fact_management=fm)
        agent.prompt_version = "v3"

        ops = [{"action": "UPDATE", "fact_id": "x"}, {"action": "DISCARD"}]
        with patch.object(
            agent, "_execute_deliberate_process_v3",
            new=AsyncMock(return_value={"operations": ops})
        ):
            msg = _make_message("consolidate", payload_extra={"messages": []})
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                await agent.execute(msg)

        repo.refresh_biographical_context_cache.assert_awaited_once()
        pb.invalidate_biographical_cache.assert_called_once_with(_ACCOUNT_ID)

    async def test_no_results_returns_failure(self):
        agent, _, _, _ = _make_agent()
        agent.prompt_version = "v2"

        with patch.object(agent, "_synthesize_session_facts_v2", new=AsyncMock(return_value=None)):
            msg = _make_message("consolidate", payload_extra={"messages": [{"role": "user", "text": "hi"}]})
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "parse" in response.error.lower()


# ==============================================================================
# TestRunConsolidationLoop
# ==============================================================================

class TestRunConsolidationLoop:

    async def test_single_turn_no_tool_calls_returns_operations(self):
        ops = [{"action": "CREATE", "fact_id": "f1"}]
        llm_response = LLMResponse(text=json.dumps({"operations": ops}), tool_calls=[])
        mock_llm = AsyncMock(return_value=llm_response)

        agent, repo, _, _ = _make_agent(mock_llm=mock_llm)

        result = await agent._run_consolidation_loop(
            user_message_text="begin",
            system_prompt="SYS",
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
        )
        assert result == ops

    async def test_tool_call_turn_then_final_answer(self):
        tool_call = ToolCall(name="count_words", args={"text": "hello world"})
        turn1 = LLMResponse(text=None, tool_calls=[tool_call])
        turn2 = LLMResponse(text=json.dumps({"operations": [{"action": "CREATE"}]}), tool_calls=[])

        call_count = 0

        async def _side_effect(request):
            nonlocal call_count
            call_count += 1
            return turn1 if call_count == 1 else turn2

        mock_llm = AsyncMock(side_effect=_side_effect)
        agent, repo, _, _ = _make_agent(mock_llm=mock_llm)

        result = await agent._run_consolidation_loop(
            user_message_text="begin",
            system_prompt="SYS",
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
        )
        assert result == [{"action": "CREATE"}]
        assert call_count == 2

    async def test_max_turns_returns_empty_list(self):
        tool_call = ToolCall(name="count_words", args={"text": "x"})
        infinite_tool_response = LLMResponse(text=None, tool_calls=[tool_call])
        mock_llm = AsyncMock(return_value=infinite_tool_response)

        agent, repo, _, _ = _make_agent(mock_llm=mock_llm)
        # Limit turns to avoid slow test
        original_max = agent.MAX_CONSOLIDATION_TURNS
        agent.__class__.MAX_CONSOLIDATION_TURNS = 2

        try:
            result = await agent._run_consolidation_loop(
                user_message_text="begin",
                system_prompt="SYS",
                user_id=_USER_ID,
                account_id=_ACCOUNT_ID,
            )
        finally:
            agent.__class__.MAX_CONSOLIDATION_TURNS = original_max

        assert result == []


# ==============================================================================
# TestExecuteFactManagementTools
# ==============================================================================

class TestExecuteFactManagementTools:

    def _make_agent_with_fm(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, _, _, _ = _make_agent(fact_management=fm)
        return agent, fm

    async def test_search_existing_facts(self):
        agent, fm = self._make_agent_with_fm()
        fm.search_existing_facts = AsyncMock(return_value=[{"fact_id": "f1"}])

        tc = ToolCall(name="search_existing_facts", args={
            "keywords": ["kw"], "primary_query": "pq", "alternative_query": "aq", "limit": 5
        })
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        assert len(results) == 1
        assert results[0].name == "search_existing_facts"
        assert json.loads(results[0].result_str) == [{"fact_id": "f1"}]

    async def test_create_fact(self):
        agent, fm = self._make_agent_with_fm()
        fm.create_fact = AsyncMock(return_value={"fact_id": "new-f", "status": "created"})

        tc = ToolCall(name="create_fact", args={
            "content": "new fact text",
            "fact_attributes": {"domain": "professional"}
        })
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        assert results[0].name == "create_fact"
        data = json.loads(results[0].result_str)
        assert data["fact_id"] == "new-f"
        # account_id and user_id injected into metadata
        called_metadata = fm.create_fact.call_args.kwargs["metadata"]
        assert called_metadata["account_id"] == _ACCOUNT_ID
        assert called_metadata["user_id"] == _USER_ID

    async def test_update_fact(self):
        agent, fm = self._make_agent_with_fm()
        fm.update_fact = AsyncMock(return_value={"status": "updated"})

        tc = ToolCall(name="update_fact", args={
            "fact_id": "existing-f",
            "updates": {"content": "revised"}
        })
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        assert results[0].name == "update_fact"
        fm.update_fact.assert_awaited_once_with(fact_id="existing-f", updates={"content": "revised"})

    async def test_merge_facts(self):
        agent, fm = self._make_agent_with_fm()
        fm.merge_facts = AsyncMock(return_value={"new_fact_id": "merged-f"})

        tc = ToolCall(name="merge_facts", args={
            "fact_ids": ["a", "b"],
            "merged_content": "merged text",
            "fact_attributes": {"domain": "personal"}
        })
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        assert results[0].name == "merge_facts"
        called_metadata = fm.merge_facts.call_args.kwargs["metadata"]
        assert called_metadata["account_id"] == _ACCOUNT_ID

    async def test_count_words(self):
        agent, _ = self._make_agent_with_fm()

        tc = ToolCall(name="count_words", args={"text": "one two three"})
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        data = json.loads(results[0].result_str)
        assert data["word_count"] == 3
        assert data["within_limit"] is True
        assert data["excess"] == 0

    async def test_unknown_tool_returns_error(self):
        agent, _ = self._make_agent_with_fm()

        tc = ToolCall(name="nonexistent_tool", args={})
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        data = json.loads(results[0].result_str)
        assert "error" in data
        assert "Unknown tool" in data["error"]

    async def test_exception_in_tool_returns_error_response(self):
        agent, fm = self._make_agent_with_fm()
        fm.create_fact = AsyncMock(side_effect=RuntimeError("db down"))

        tc = ToolCall(name="create_fact", args={"content": "x", "fact_attributes": {}})
        results = await agent._execute_fact_management_tools([tc], _USER_ID, _ACCOUNT_ID)

        data = json.loads(results[0].result_str)
        assert "error" in data
        assert "db down" in data["error"]


# ==============================================================================
# TestBuildConversationText
# ==============================================================================

class TestBuildConversationText:

    def _agent(self):
        agent, _, _, _ = _make_agent()
        return agent

    def test_user_role(self):
        agent = self._agent()
        result = agent._build_conversation_text([{"role": "user", "text": "hello"}])
        assert "USER: hello" in result

    def test_non_user_role_mapped_to_assistant(self):
        agent = self._agent()
        result = agent._build_conversation_text([{"role": "model", "text": "world"}])
        assert "ASSISTANT: world" in result

    def test_message_with_parts_no_text(self):
        agent = self._agent()
        msg = {"role": "user", "parts": [{"text": "part one"}, {"text": "part two"}]}
        result = agent._build_conversation_text([msg])
        assert "part one part two" in result

    def test_with_float_timestamp(self):
        agent = self._agent()
        import time
        ts = time.time()
        result = agent._build_conversation_text([{"role": "user", "text": "hi", "timestamp": ts}])
        assert "USER (" in result
        assert "hi" in result

    def test_with_iso_timestamp(self):
        agent = self._agent()
        result = agent._build_conversation_text([
            {"role": "user", "text": "hi", "timestamp": "2026-01-15T10:00:00"}
        ])
        assert "2026-01-15 10:00:00" in result

    def test_bad_timestamp_falls_back_gracefully(self):
        agent = self._agent()
        result = agent._build_conversation_text([
            {"role": "user", "text": "msg", "timestamp": "not-a-date"}
        ])
        assert "USER: msg" in result

    def test_no_timestamp(self):
        agent = self._agent()
        result = agent._build_conversation_text([{"role": "user", "text": "plain"}])
        assert result == "USER: plain\n"


# ==============================================================================
# TestPrepareStructuredConversation
# ==============================================================================

class TestPrepareStructuredConversation:

    def _agent(self):
        agent, _, _, _ = _make_agent()
        return agent

    def test_basic_roles(self):
        agent = self._agent()
        msgs = [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "world"},
        ]
        result = agent._prepare_structured_conversation(msgs)
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[0]["content"] == "hello"

    def test_parts_text_extraction(self):
        agent = self._agent()
        msgs = [{"role": "user", "parts": [{"text": "a"}, {"text": "b"}]}]
        result = agent._prepare_structured_conversation(msgs)
        assert result[0]["content"] == "a b"

    def test_numeric_timestamp(self):
        agent = self._agent()
        msgs = [{"role": "user", "text": "hi", "timestamp": 1700000000.0}]
        result = agent._prepare_structured_conversation(msgs)
        assert result[0]["timestamp"] != ""

    def test_iso_timestamp(self):
        agent = self._agent()
        msgs = [{"role": "user", "text": "hi", "created_at": "2026-03-15T12:00:00Z"}]
        result = agent._prepare_structured_conversation(msgs)
        assert "Mar 15" in result[0]["timestamp"]

    def test_bad_timestamp_gives_empty_string(self):
        agent = self._agent()
        msgs = [{"role": "user", "text": "hi", "timestamp": "INVALID"}]
        result = agent._prepare_structured_conversation(msgs)
        assert result[0]["timestamp"] == ""


# ==============================================================================
# TestParseConsolidationResults
# ==============================================================================

class TestParseConsolidationResults:

    def _agent(self):
        agent, _, _, _ = _make_agent()
        return agent

    def test_json_in_markdown_block(self):
        agent = self._agent()
        text = '```json\n{"new_facts": [{"id": "1", "text": "f"}], "new_anchors": []}\n```'
        result = agent._parse_consolidation_results(text)
        assert result["new_facts"][0]["id"] == "1"
        assert result["new_anchors"] == []

    def test_plain_json(self):
        agent = self._agent()
        data = {"new_facts": [], "new_anchors": [{"id": "a1"}]}
        result = agent._parse_consolidation_results(json.dumps(data))
        assert result["new_anchors"][0]["id"] == "a1"

    def test_invalid_json_returns_empty_dict(self):
        agent = self._agent()
        result = agent._parse_consolidation_results("not json at all")
        assert result == {}


# ==============================================================================
# TestSanitizeIds
# ==============================================================================

class TestSanitizeIds:

    def _agent(self):
        agent, _, _, _ = _make_agent()
        return agent

    def test_normal_item_passes_through(self):
        agent = self._agent()
        items = [{"id": "fact-1", "text": "some text"}]
        result = agent._sanitize_ids(items)
        assert len(result) == 1
        assert result[0]["id"] == "fact-1"

    def test_string_item_skipped(self):
        agent = self._agent()
        result = agent._sanitize_ids(["not a dict"])
        assert result == []

    def test_non_dict_non_string_skipped(self):
        agent = self._agent()
        result = agent._sanitize_ids([42, None])
        assert result == []

    def test_item_without_id_skipped(self):
        agent = self._agent()
        result = agent._sanitize_ids([{"text": "no id here"}])
        assert result == []

    def test_duplicate_id_renamed(self):
        agent = self._agent()
        items = [
            {"id": "x", "text": "first"},
            {"id": "x", "text": "second"},
        ]
        result = agent._sanitize_ids(items)
        assert len(result) == 2
        assert result[0]["id"] == "x"
        assert result[1]["id"] == "x_a"


# ==============================================================================
# TestParseOperationsReport
# ==============================================================================

class TestParseOperationsReport:

    def _agent(self):
        agent, _, _, _ = _make_agent()
        return agent

    def test_json_in_markdown_block(self):
        agent = self._agent()
        ops = [{"action": "CREATE", "fact_id": "f1"}]
        text = f'```json\n{json.dumps({"operations": ops})}\n```'
        result = agent._parse_operations_report(text)
        assert result == ops

    def test_plain_json(self):
        agent = self._agent()
        ops = [{"action": "UPDATE", "fact_id": "f2"}]
        result = agent._parse_operations_report(json.dumps({"operations": ops}))
        assert result == ops

    def test_embedded_json_with_operations_key(self):
        agent = self._agent()
        ops = [{"action": "DISCARD"}]
        text = f'Here is the report: {json.dumps({"operations": ops})} — done.'
        result = agent._parse_operations_report(text)
        assert result == ops

    def test_plain_text_returns_empty_list(self):
        agent = self._agent()
        result = agent._parse_operations_report("All done, nothing to report.")
        assert result == []

    def test_json_decode_error_returns_empty_list(self):
        agent = self._agent()
        result = agent._parse_operations_report("```json\n{bad json\n```")
        assert result == []


# ==============================================================================
# TestBuildClusterMessage
# ==============================================================================

class TestBuildClusterMessage:

    def test_builds_string_with_fact_fields(self):
        cluster = [
            {"fact_id": "f1", "content": "The sky is blue", "similarity": 0.95},
        ]
        result = ConsolidationAgent._build_cluster_message(cluster)
        assert "FACT CLUSTER REVIEW" in result
        assert "f1" in result
        assert "The sky is blue" in result
        assert "0.95" in result

    def test_multiple_facts_numbered(self):
        cluster = [
            {"fact_id": "f1", "content": "fact one", "similarity": 0.9},
            {"fact_id": "f2", "content": "fact two", "similarity": 0.8},
        ]
        result = ConsolidationAgent._build_cluster_message(cluster)
        assert "1. " in result
        assert "2. " in result


# ==============================================================================
# TestHandleConsolidateZeroFacts  (lines 356, 378-379)
# ==============================================================================

class TestHandleConsolidateZeroFacts:
    """v2 path: messages + observations both provided, synthesize returns empty facts.
    Covers line 356 (log zero facts) and lines 378-379 (archive observations)."""

    async def test_zero_facts_with_messages_and_observations_archives_observations(self):
        # v2 path requires prompt_version != "v3" OR no fact_management
        agent, repo, fact_write, pb = _make_agent(fact_management=None)
        # Force v2 path by ensuring prompt_version is not "v3"
        agent.prompt_version = "v2"

        # _synthesize_session_facts_v2 returns empty facts
        empty_result = {"new_facts": [], "new_anchors": []}
        agent._synthesize_session_facts_v2 = AsyncMock(return_value=empty_result)

        messages = [{"role": "user", "text": "hello"}]
        observations = [{"id": "obs-1", "text": "some observation"}]

        msg = _make_message(
            "consolidate",
            payload_extra={"messages": messages, "observations": observations},
        )

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate(msg, _USER_ID, _ACCOUNT_ID, [], time.time())

        # Should succeed (zero facts is valid for messages path)
        assert response.status == AgentStatus.SUCCESS
        # Observations should have been archived (lines 378-379)
        repo.archive_observations.assert_called_once()
        call_args = repo.archive_observations.call_args
        assert call_args[0][0] == ["obs-1"]

    async def test_zero_facts_with_messages_only_does_not_archive(self):
        agent, repo, fact_write, pb = _make_agent(fact_management=None)
        agent.prompt_version = "v2"

        empty_result = {"new_facts": [], "new_anchors": []}
        agent._synthesize_session_facts_v2 = AsyncMock(return_value=empty_result)

        messages = [{"role": "user", "text": "hello"}]
        msg = _make_message("consolidate", payload_extra={"messages": messages})

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate(msg, _USER_ID, _ACCOUNT_ID, [], time.time())

        assert response.status == AgentStatus.SUCCESS
        repo.archive_observations.assert_not_called()


# ==============================================================================
# TestHandleConsolidateException  (lines 417-419)
# ==============================================================================

class TestHandleConsolidateException:
    """Exception handler in _handle_consolidate — trigger via RuntimeError in v3 path."""

    async def test_runtime_error_returns_failure(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)
        agent.prompt_version = "v3"

        agent._execute_deliberate_process_v3 = AsyncMock(
            side_effect=RuntimeError("deliberate process exploded")
        )

        msg = _make_message(
            "consolidate",
            payload_extra={"messages": [{"role": "user", "text": "hi"}]},
        )

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate(msg, _USER_ID, _ACCOUNT_ID, [], time.time())

        assert response.status == AgentStatus.FAILED
        assert "deliberate process exploded" in (response.error or "")


# ==============================================================================
# TestHandleConsolidateClusterDirect  (lines 438-501)
# ==============================================================================

class TestHandleConsolidateClusterDirect:
    """Direct calls to _handle_consolidate_cluster."""

    async def test_explicit_cluster_runs_loop_and_succeeds(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        cluster = [{"fact_id": "f1", "content": "user lives in Spain", "similarity": 0.9}]
        ops = [{"action": "UPDATE", "fact_id": "f1", "reason": "enriched"}]
        agent._run_consolidation_loop = AsyncMock(return_value=ops)

        msg = _make_message("consolidate_cluster", payload_extra={"cluster": cluster})

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_cluster(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        assert response.result["operations"] == 1
        assert response.result["facts_affected"] == 1
        repo.refresh_biographical_context_cache.assert_called_once()

    async def test_auto_fetch_populates_cluster(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        fact = MagicMock()
        fact.id = "f99"
        fact.text = "long fact text here"
        repo.get_longest_facts = AsyncMock(return_value=[fact])

        agent._run_consolidation_loop = AsyncMock(return_value=[])

        msg = _make_message("consolidate_cluster", payload_extra={"limit": 5})

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_cluster(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        repo.get_longest_facts.assert_called_once_with(_ACCOUNT_ID, limit=5)

    async def test_auto_fetch_empty_returns_no_facts_to_review(self):
        agent, repo, fact_write, pb = _make_agent(fact_management=None)
        repo.get_longest_facts = AsyncMock(return_value=[])

        msg = _make_message("consolidate_cluster")

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_cluster(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        assert response.result["message"] == "No facts to review"

    async def test_exception_returns_failure(self):
        agent, repo, fact_write, pb = _make_agent(fact_management=None)
        repo.get_longest_facts = AsyncMock(side_effect=RuntimeError("db error"))

        msg = _make_message("consolidate_cluster")

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_cluster(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.FAILED
        assert "db error" in (response.error or "")


# ==============================================================================
# TestHandleConsolidateEmailDirect  (lines 523-607)
# ==============================================================================

class TestHandleConsolidateEmailDirect:
    """Direct calls to _handle_consolidate_email."""

    async def test_no_repo_returns_success_immediately(self):
        agent, repo, fact_write, pb = _make_agent(indexed_email_repo=None)

        msg = _make_message("consolidate_email")

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_email(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        assert response.result["message"] == "No email repo"

    async def test_with_emails_batch_processes_and_marks_consolidated(self):
        email_repo = AsyncMock()
        agent, repo, fact_write, pb = _make_agent(indexed_email_repo=email_repo)

        email = MagicMock()
        email.email_id = "em-1"
        email.text = "user subscribed to newsletter"
        email.category = "NEWSLETTER"
        email.tags = ["newsletter"]
        email.email_date = MagicMock()
        email.email_date.strftime = MagicMock(return_value="2026-01-01")
        email.from_address = "news@example.com"
        email.subject = "Weekly digest"
        email.attachments = []
        email.metadata = {}

        # First call returns one email; second call returns empty (no more batches)
        email_repo.get_unconsolidated_batch = AsyncMock(side_effect=[[email], []])
        email_repo.mark_consolidated = AsyncMock()

        ops = [{"action": "CREATE", "fact_id": "new-1", "reason": "new newsletter fact"}]
        agent._run_consolidation_loop = AsyncMock(return_value=ops)

        msg = _make_message(
            "consolidate_email",
            payload_extra={"number_of_batches": 2, "batch_size": 10},
        )

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_email(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        assert response.result["batches_processed"] == 1
        assert response.result["facts_affected"] == 1
        email_repo.mark_consolidated.assert_called_once()
        repo.refresh_biographical_context_cache.assert_called_once()

    async def test_empty_batch_on_first_pass_returns_zero_batches(self):
        email_repo = AsyncMock()
        agent, repo, fact_write, pb = _make_agent(indexed_email_repo=email_repo)

        email_repo.get_unconsolidated_batch = AsyncMock(return_value=[])

        msg = _make_message("consolidate_email", payload_extra={"number_of_batches": 3})

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_email(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.SUCCESS
        assert response.result["batches_processed"] == 0
        # No cache refresh when zero batches
        repo.refresh_biographical_context_cache.assert_not_called()


# ==============================================================================
# TestHandleConsolidateFullDirect  (lines 629-651)
# ==============================================================================

class TestHandleConsolidateFullDirect:
    """Direct calls to _handle_consolidate_full."""

    async def test_stage1_failure_aborts_pipeline(self):
        agent, repo, fact_write, pb = _make_agent(fact_management=None)

        failure_response = MagicMock()
        failure_response.status = AgentStatus.FAILED
        failure_response.error = "stage1 failed"
        agent._handle_consolidate = AsyncMock(return_value=failure_response)

        msg = _make_message(
            "consolidate_full",
            payload_extra={"messages": [{"role": "user", "text": "hello"}]},
        )

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_full(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        assert response.status == AgentStatus.FAILED
        assert response.error == "stage1 failed"

    async def test_stage1_success_proceeds_to_email(self):
        agent, repo, fact_write, pb = _make_agent(indexed_email_repo=None)

        success_response = MagicMock()
        success_response.status = AgentStatus.SUCCESS
        success_response.result = {"operations": 3, "facts_affected": 2}
        agent._handle_consolidate = AsyncMock(return_value=success_response)

        msg = _make_message(
            "consolidate_full",
            payload_extra={"messages": [{"role": "user", "text": "hello"}]},
        )

        import time
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            response = await agent._handle_consolidate_full(
                msg, _USER_ID, _ACCOUNT_ID, [], time.time()
            )

        # Email stage returns "No email repo" success
        assert response.status == AgentStatus.SUCCESS
        assert response.result["stage1_operations"] == 3
        assert response.result["stage1_facts_affected"] == 2
        assert response.result["email_batches"] == 0


# ==============================================================================
# TestFormatEmailCandidates  (lines 667-684)
# ==============================================================================

class TestFormatEmailCandidates:
    """_format_email_candidates static method."""

    def test_basic_formatting(self):
        email = MagicMock()
        email.email_id = "em-abc"
        email.text = "user bought a laptop"
        email.category = "PURCHASE"
        email.tags = ["purchase", "tech"]
        email.email_date = MagicMock()
        email.email_date.strftime = MagicMock(return_value="2026-03-01")
        email.from_address = "shop@store.com"
        email.subject = "Your order #123"
        email.attachments = []
        email.metadata = {}

        result = ConsolidationAgent._format_email_candidates([email])

        assert "em-abc" in result
        assert "user bought a laptop" in result
        assert "2026-03-01" in result
        assert "shop@store.com" in result
        assert "Your order #123" in result
        assert result.startswith("1. ")

    def test_attachments_included_when_present(self):
        email = MagicMock()
        email.email_id = "em-1"
        email.text = "invoice attached"
        email.category = "INVOICE"
        email.tags = []
        email.email_date = MagicMock()
        email.email_date.strftime = MagicMock(return_value="2026-01-15")
        email.from_address = "billing@acme.com"
        email.subject = "Invoice Q1"
        email.attachments = ["invoice.pdf"]
        email.metadata = {}

        result = ConsolidationAgent._format_email_candidates([email])
        assert "invoice.pdf" in result

    def test_multiple_emails_numbered(self):
        emails = []
        for i in range(3):
            e = MagicMock()
            e.email_id = f"em-{i}"
            e.text = f"fact {i}"
            e.category = "INFO"
            e.tags = []
            e.email_date = MagicMock()
            e.email_date.strftime = MagicMock(return_value="2026-01-01")
            e.from_address = "x@y.com"
            e.subject = f"Subject {i}"
            e.attachments = []
            e.metadata = {}
            emails.append(e)

        result = ConsolidationAgent._format_email_candidates(emails)
        assert "1. " in result
        assert "2. " in result
        assert "3. " in result


# ==============================================================================
# TestExecuteDeliberateProcessV3  (lines 703-777)
# ==============================================================================

class TestExecuteDeliberateProcessV3:
    """_execute_deliberate_process_v3 — INLINE_CLUSTER_REVIEW=False, no changed, with cluster review."""

    async def test_inline_cluster_review_disabled_returns_ops1_only(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)
        agent.INLINE_CLUSTER_REVIEW = False

        ops1 = [{"action": "CREATE", "fact_id": "new-1"}]
        agent._run_consolidation_loop = AsyncMock(return_value=ops1)

        messages = [{"role": "user", "text": "I moved to Spain"}]
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            result = await agent._execute_deliberate_process_v3(
                messages, _USER_ID, _ACCOUNT_ID, []
            )

        assert result == {"operations": ops1}
        # Only one loop call (no stage 2)
        agent._run_consolidation_loop.assert_called_once()

    async def test_no_changed_facts_skips_stage2(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)
        agent.INLINE_CLUSTER_REVIEW = True

        ops1 = [{"action": "DISCARD", "reason": "already known"}]
        agent._run_consolidation_loop = AsyncMock(return_value=ops1)
        # Tracker.changed will be empty since fm methods are not called via tool loop

        messages = [{"role": "user", "text": "nothing new"}]
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            result = await agent._execute_deliberate_process_v3(
                messages, _USER_ID, _ACCOUNT_ID, []
            )

        assert result == {"operations": ops1}
        agent._run_consolidation_loop.assert_called_once()

    async def test_with_cluster_review_runs_stage2(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)
        agent.INLINE_CLUSTER_REVIEW = True

        ops1 = [{"action": "CREATE", "fact_id": "f-new"}]
        ops2 = [{"action": "UPDATE", "fact_id": "f-old"}]

        call_count = [0]

        async def mock_loop(*args, **kwargs):
            call_count[0] += 1
            return ops1 if call_count[0] == 1 else ops2

        agent._run_consolidation_loop = mock_loop

        cluster = [{"fact_id": "f-new", "content": "something", "similarity": 0.8}]
        agent._build_review_cluster = AsyncMock(return_value=cluster)

        # Simulate tracker.changed having entries by patching _TrackingFactManagement
        original_init = _TrackingFactManagement.__init__

        def patched_init(self_tracker, real):
            original_init(self_tracker, real)
            self_tracker.changed = [("f-new", "something")]

        with patch.object(_TrackingFactManagement, "__init__", patched_init):
            messages = [{"role": "user", "text": "I run a startup"}]
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                result = await agent._execute_deliberate_process_v3(
                    messages, _USER_ID, _ACCOUNT_ID, []
                )

        assert result == {"operations": ops1 + ops2}
        assert call_count[0] == 2

    async def test_empty_cluster_from_build_review_skips_stage2(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)
        agent.INLINE_CLUSTER_REVIEW = True

        ops1 = [{"action": "CREATE", "fact_id": "f-new"}]
        agent._run_consolidation_loop = AsyncMock(return_value=ops1)
        agent._build_review_cluster = AsyncMock(return_value=[])

        original_init = _TrackingFactManagement.__init__

        def patched_init(self_tracker, real):
            original_init(self_tracker, real)
            self_tracker.changed = [("f-new", "content")]

        with patch.object(_TrackingFactManagement, "__init__", patched_init):
            messages = [{"role": "user", "text": "fact here"}]
            with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
                result = await agent._execute_deliberate_process_v3(
                    messages, _USER_ID, _ACCOUNT_ID, []
                )

        assert result == {"operations": ops1}
        agent._run_consolidation_loop.assert_called_once()


# ==============================================================================
# TestRunConsolidationLoopRawContent  (line 821)
# ==============================================================================

class TestRunConsolidationLoopRawContent:
    """_run_consolidation_loop uses raw_content branch when response has raw_content set."""

    async def test_raw_content_appended_to_history(self):
        fm = AsyncMock(spec=FactManagementPort)
        fm.search_existing_facts = AsyncMock(return_value=[])
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        tool_call = ToolCall(name="search_existing_facts", args={"keywords": ["test"], "primary_query": "test"})
        raw_content_obj = [{"type": "text", "text": "raw thinking block"}]

        # Turn 1: tool call with raw_content; Turn 2: final report
        turn1_response = LLMResponse(
            text="",
            tool_calls=[tool_call],
            raw_content=raw_content_obj,
        )
        turn2_response = LLMResponse(
            text=json.dumps({"operations": []}),
            tool_calls=[],
        )

        mock_llm = AsyncMock(side_effect=[turn1_response, turn2_response])
        agent._llm.generate_content = mock_llm

        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            ops = await agent._run_consolidation_loop(
                user_message_text="consolidate",
                system_prompt="SYSTEM",
                user_id=_USER_ID,
                account_id=_ACCOUNT_ID,
            )

        assert ops == []
        # Two LLM calls were made
        assert mock_llm.call_count == 2
        # Verify the history message after turn 1 used raw_content (parts=[])
        second_call_args = mock_llm.call_args_list[1]
        # generate_content is called with request= as a keyword argument
        request_arg = second_call_args.kwargs.get("request") or second_call_args[0][0]
        model_message = request_arg.messages[1]  # index 0=user, 1=model, 2=tool_response
        assert model_message.role == "model"
        assert model_message.raw_content == raw_content_obj
        assert model_message.parts == []


# ==============================================================================
# TestBuildReviewCluster  (lines 862-898)
# ==============================================================================

class TestBuildReviewCluster:
    """_build_review_cluster — basic call, empty contents."""

    async def test_empty_changed_returns_empty_list(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        result = await agent._build_review_cluster([], _ACCOUNT_ID)
        assert result == []

    async def test_changed_with_empty_content_returns_empty_list(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        # All contents are empty strings / None
        changed = [("f1", ""), ("f2", None)]
        result = await agent._build_review_cluster(changed, _ACCOUNT_ID)
        assert result == []

    async def test_basic_search_and_dedup(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        facts_result = [
            {"fact_id": "f1", "content": "user lives in Spain", "similarity": 0.9},
            {"fact_id": "f2", "content": "user is a developer", "similarity": 0.7},
        ]
        fm.search_existing_facts = AsyncMock(return_value=facts_result)

        changed = [("new-1", "user moved to Spain from Ukraine")]
        result = await agent._build_review_cluster(changed, _ACCOUNT_ID)

        assert len(result) == 2
        fm.search_existing_facts.assert_called_once()

    async def test_dedup_keeps_highest_similarity(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        # Two searches return overlapping fact_id "f1" with different similarities
        search1 = [{"fact_id": "f1", "content": "short", "similarity": 0.6}]
        search2 = [{"fact_id": "f1", "content": "short", "similarity": 0.95}]
        fm.search_existing_facts = AsyncMock(side_effect=[search1, search2])

        changed = [("new-1", "first change content here"), ("new-2", "second change content here")]
        result = await agent._build_review_cluster(changed, _ACCOUNT_ID)

        # f1 should appear once, with the higher similarity
        assert len(result) == 1
        assert result[0]["similarity"] == 0.95


# ==============================================================================
# TestExecuteFactManagementToolsParallel  (lines 1041-1044)
# ==============================================================================

class TestExecuteFactManagementToolsParallel:
    """Parallel tool dispatch when 2+ tool calls are provided."""

    async def test_two_tools_dispatched_concurrently(self):
        fm = AsyncMock(spec=FactManagementPort)
        fm.search_existing_facts = AsyncMock(return_value=[{"fact_id": "f1", "content": "x", "similarity": 0.9}])
        agent, repo, fact_write, pb = _make_agent(fact_management=fm)

        tc1 = ToolCall(name="search_existing_facts", args={"keywords": ["k1"], "primary_query": "query one"})
        tc2 = ToolCall(name="search_existing_facts", args={"keywords": ["k2"], "primary_query": "query two"})

        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            responses = await agent._execute_fact_management_tools(
                tool_calls=[tc1, tc2],
                user_id=_USER_ID,
                account_id=_ACCOUNT_ID,
            )

        assert len(responses) == 2
        assert responses[0].name == "search_existing_facts"
        assert responses[1].name == "search_existing_facts"
        assert fm.search_existing_facts.call_count == 2


# ==============================================================================
# TestParseOperationsReportJsonDecodeError  (lines 1228-1230)
# ==============================================================================

class TestParseOperationsReportJsonDecodeError:
    """Outer JSONDecodeError handler: markdown block matches but JSON inside is invalid."""

    def test_markdown_block_with_invalid_json_returns_empty_list(self):
        # Regex r"```json\s*(\{.*?\})\s*```" matches {bad: json}
        # json.loads("{bad: json}") raises JSONDecodeError → outer handler returns []
        agent, _, _, _ = _make_agent()
        text = "```json\n{bad: json}\n```"
        result = agent._parse_operations_report(text)
        assert result == []

    def test_markdown_block_with_missing_quotes_returns_empty_list(self):
        agent, _, _, _ = _make_agent()
        text = "```json\n{operations: [invalid]}\n```"
        result = agent._parse_operations_report(text)
        assert result == []


# ==============================================================================
# TestLegacyMethods  (lines 1314-1316, 1324-1326, 1337-1378)
# ==============================================================================

class TestLegacyMethods:
    """_format_existing_anchors, _get_existing_anchors_list, _synthesize_session_facts_v2."""

    async def test_format_existing_anchors_returns_comma_joined(self):
        agent, repo, fact_write, pb = _make_agent()

        anchor1 = MagicMock()
        anchor1.text = "I am a software engineer"
        anchor2 = MagicMock()
        anchor2.text = "I live in Valencia"
        repo.get_active_facts = AsyncMock(return_value=[anchor1, anchor2])

        result = await agent._format_existing_anchors(_USER_ID)

        repo.get_active_facts.assert_called_once_with(_USER_ID, tags=["anchor"])
        assert '"I am a software engineer"' in result
        assert '"I live in Valencia"' in result
        assert ",\n" in result

    async def test_format_existing_anchors_empty_list(self):
        agent, repo, fact_write, pb = _make_agent()
        repo.get_active_facts = AsyncMock(return_value=[])

        result = await agent._format_existing_anchors(_USER_ID)
        assert result == ""

    async def test_get_existing_anchors_list_filters_anchors(self):
        agent, repo, fact_write, pb = _make_agent()

        repo.get_biographical_context_cached = AsyncMock(return_value=[
            {"text": "I am a developer", "tags": ["anchor", "profession"]},
            {"text": "I like coffee", "tags": ["preference"]},
            {"text": "I live in Spain", "tags": ["anchor", "location"]},
        ])

        result = await agent._get_existing_anchors_list(_ACCOUNT_ID)

        assert result == ["I am a developer", "I live in Spain"]
        repo.get_biographical_context_cached.assert_called_once_with(owner_id=_ACCOUNT_ID, limit=100)

    async def test_get_existing_anchors_list_empty_cache(self):
        agent, repo, fact_write, pb = _make_agent()
        repo.get_biographical_context_cached = AsyncMock(return_value=[])

        result = await agent._get_existing_anchors_list(_ACCOUNT_ID)
        assert result == []

    async def test_synthesize_session_facts_v2_returns_parsed_result(self):
        agent, repo, fact_write, pb = _make_agent()

        repo.get_biographical_context_cached = AsyncMock(return_value=[])
        llm_result = {"new_facts": [{"id": "f1", "text": "fact one"}], "new_anchors": []}
        mock_llm = AsyncMock(
            return_value=LLMResponse(text=json.dumps(llm_result))
        )
        agent._llm.generate_content = mock_llm

        messages = [{"role": "user", "text": "I started a new job"}]
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            result = await agent._synthesize_session_facts_v2(
                messages, _USER_ID, _ACCOUNT_ID, []
            )

        assert result["new_facts"] == llm_result["new_facts"]
        assert result["new_anchors"] == []
        mock_llm.assert_called_once()

    async def test_synthesize_session_facts_v2_appends_anchors_to_bio(self):
        agent, repo, fact_write, pb = _make_agent()

        repo.get_biographical_context_cached = AsyncMock(return_value=[
            {"text": "anchor text", "tags": ["anchor"]}
        ])
        llm_result = {"new_facts": [], "new_anchors": []}
        mock_llm = AsyncMock(return_value=LLMResponse(text=json.dumps(llm_result)))
        agent._llm.generate_content = mock_llm

        messages = [{"role": "user", "text": "nothing new"}]
        with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
            await agent._synthesize_session_facts_v2(messages, _USER_ID, _ACCOUNT_ID, [])

        # prompt_builder was called — verify biographical_facts arg includes anchors
        call_kwargs = pb.build_for_agent.call_args[1]
        bio_facts = call_kwargs.get("biographical_facts", [])
        anchor_entry = next((f for f in bio_facts if "Existing anchors" in f.get("text", "")), None)
        assert anchor_entry is not None


# ==============================================================================
# TestGetAlternativeAgents  (line 1438)
# ==============================================================================

class TestGetAlternativeAgents:
    """_get_alternative_agents returns empty list."""

    def test_returns_empty_list(self):
        agent, _, _, _ = _make_agent()
        result = agent._get_alternative_agents()
        assert result == []


# ==============================================================================
# TestFormatEmailCandidatesMetadata  (line 682)
# ==============================================================================

class TestFormatEmailCandidatesMetadata:
    """_format_email_candidates includes metadata when truthy."""

    def test_metadata_included_when_present(self):
        email = MagicMock()
        email.email_id = "em-meta"
        email.text = "subscription renewed"
        email.category = "SUBSCRIPTION"
        email.tags = ["subscription"]
        email.email_date = MagicMock()
        email.email_date.strftime = MagicMock(return_value="2026-03-15")
        email.from_address = "billing@service.com"
        email.subject = "Renewal notice"
        email.attachments = []
        email.metadata = {"amount": 9.99, "currency": "USD"}

        result = ConsolidationAgent._format_email_candidates([email])

        assert "amount" in result
        assert "9.99" in result


# ==============================================================================
# TestBuildReviewClusterNoFactId  (line 884)
# ==============================================================================

class TestBuildReviewClusterNoFactId:
    """_build_review_cluster skips facts without fact_id."""

    async def test_facts_without_fact_id_skipped(self):
        fm = AsyncMock(spec=FactManagementPort)
        agent, _, _, _ = _make_agent(fact_management=fm)

        # Search returns one fact without fact_id — should be skipped
        fm.search_existing_facts = AsyncMock(return_value=[
            {"content": "no id here", "similarity": 0.9},  # no fact_id
            {"fact_id": "f1", "content": "has id", "similarity": 0.8},
        ])

        changed = [("new-1", "some long enough content here")]
        result = await agent._build_review_cluster(changed, _ACCOUNT_ID)

        assert len(result) == 1
        assert result[0]["fact_id"] == "f1"

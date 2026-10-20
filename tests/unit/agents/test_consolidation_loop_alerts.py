"""Ops alerts of the consolidation loop: 📊 per-pass stats (temporary knob) and the permanent
🚨 turn-limit alert. See decisions/sonnet55_adaptive_thinking_stall.md."""

import json
from unittest.mock import AsyncMock

import pytest

from src.ports.alert_sink import AlertSinkPort
from src.domain.consolidation_pass_stats import ConsolidationPassStats, stats_alerts_enabled
from src.ports.llm_port import LLMResponse, ToolCall

from tests.unit.agents.test_consolidation_agent import _make_agent, _USER_ID, _ACCOUNT_ID

_FULL_USER_ID = "abcdef1234567890-secret-tail"
_REPORT = LLMResponse(text=json.dumps({"operations": [{"action": "CREATE"}, {"action": "UPDATE"}]}), tool_calls=[])


def _tools(*calls):
    return LLMResponse(text=None, tool_calls=[ToolCall(name=n, args=a) for n, a in calls])


def _agent_with(responses, sink, stats=True):
    it = iter(responses)

    async def _side(request):
        return next(it)

    agent, repo, _, _ = _make_agent(mock_llm=AsyncMock(side_effect=_side))
    agent._alert_sink = sink
    agent._stats_alerts = stats
    agent._execute_fact_management_tools = AsyncMock(return_value=[])
    return agent


async def _run(agent, user_id=_USER_ID, label="stage1"):
    return await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS",
        user_id=user_id, account_id=_ACCOUNT_ID, pass_label=label,
    )


@pytest.fixture
def limited_turns():
    from src.agents.consolidation_agent import ConsolidationAgent
    original = ConsolidationAgent.MAX_CONSOLIDATION_TURNS
    ConsolidationAgent.MAX_CONSOLIDATION_TURNS = 3
    yield
    ConsolidationAgent.MAX_CONSOLIDATION_TURNS = original


def _stalling(n=3):
    return [_tools(("count_words", {"text": "x"})) for _ in range(n)]


class TestStatsAlert:

    async def test_pass_with_report_posts_one_stats_line(self):
        sink = AsyncMock(spec=AlertSinkPort)
        agent = _agent_with([
            _tools(("search_existing_facts", {"q": "a"}), ("search_existing_facts", {"q": "b"}),
                   ("search_existing_facts", {"q": "c"})),
            _tools(("create_fact", {"c": "1"}), ("create_fact", {"c": "2"}), ("update_fact", {"id": "f"})),
            _REPORT,
        ], sink)

        result = await _run(agent)

        assert result == [{"action": "CREATE"}, {"action": "UPDATE"}]
        sink.post.assert_awaited_once()
        text = sink.post.await_args.args[0]
        print(text)
        assert text.startswith("📊")
        assert "`stage1`" in text
        assert "turns 3/" in text  # 2 tool turns + the final report turn
        assert "search×3" in text and "create×2" in text and "update×1" in text
        assert "repeats 0" in text
        assert "writes 3" in text
        assert "report ops 2" in text
        assert "t1 search×3 · t2 create×2 update×1" in text

    async def test_stats_off_suppresses_stats_but_not_limit_alert(self, limited_turns):
        sink = AsyncMock(spec=AlertSinkPort)
        await _run(_agent_with([_tools(("count_words", {"text": "x"})), _REPORT], sink, stats=False))
        sink.post.assert_not_awaited()

        sink2 = AsyncMock(spec=AlertSinkPort)
        assert await _run(_agent_with(_stalling(), sink2, stats=False)) == []
        sink2.post.assert_awaited_once()
        assert sink2.post.await_args.args[0].startswith("🚨")

    @pytest.mark.parametrize("value", ["0", "false", "No", "OFF", " off "])
    def test_off_spellings(self, value):
        assert stats_alerts_enabled(value) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_on_spellings(self, value):
        assert stats_alerts_enabled(value) is True


class TestLimitAlert:

    async def test_max_turns_posts_one_alert_and_returns_empty(self, limited_turns):
        sink = AsyncMock(spec=AlertSinkPort)
        agent = _agent_with([
            _tools(("create_fact", {"c": "1"})),
            _tools(("count_words", {"text": "x"})),
            _tools(("count_words", {"text": "x"})),
        ], sink)

        assert await _run(agent, label="cluster") == []

        sink.post.assert_awaited_once()
        text = sink.post.await_args.args[0]
        print(text)
        assert text.startswith("🚨")
        assert "turn limit" in text and "without a final report" in text
        assert "`cluster`" in text
        assert "turns 3/3" in text
        assert "writes so far 1" in text
        assert "repeats 1" in text
        assert "t1 create×1 · t2 count×1 · t3 count×1" in text


class TestBestEffort:

    async def test_no_sink_is_fine(self, limited_turns):
        agent = _agent_with(_stalling(), None)
        assert await _run(agent) == []
        ok = _agent_with([_REPORT], None)
        assert await _run(ok) == [{"action": "CREATE"}, {"action": "UPDATE"}]

    async def test_failing_sink_does_not_change_result(self, limited_turns):
        sink = AsyncMock(spec=AlertSinkPort)
        sink.post.side_effect = RuntimeError("slack down")
        assert await _run(_agent_with(_stalling(), sink)) == []
        assert await _run(_agent_with([_REPORT], sink)) == [{"action": "CREATE"}, {"action": "UPDATE"}]
        assert sink.post.await_count == 2


class TestPrivacy:

    async def test_only_user_prefix_in_text(self, limited_turns):
        sink = AsyncMock(spec=AlertSinkPort)
        await _run(_agent_with([_tools(("create_fact", {"content": "private fact"})), _REPORT], sink),
                   user_id=_FULL_USER_ID)
        await _run(_agent_with(_stalling(), sink), user_id=_FULL_USER_ID)
        assert sink.post.await_count == 2
        for call in sink.post.await_args_list:
            text = call.args[0]
            assert "abcdef12" in text
            assert _FULL_USER_ID not in text
            assert "abcdef123" not in text
            assert "private fact" not in text


class TestPassStats:

    def test_repeats_counts_identical_consecutive_turns_order_insensitive(self):
        s = ConsolidationPassStats("p", "m", "medium", 15, "u" * 20)
        s.record_turn([("a", {"x": 1}), ("b", {"y": 2})])
        s.record_turn([("b", {"y": 2}), ("a", {"x": 1})])  # same set, other order
        s.record_turn([("a", {"x": 2})])                    # different args
        s.record_turn([("a", {"x": 2})])                    # repeat again
        assert s.repeats == 2
        assert s.turns_used == 4
        assert s.counts == {"a": 4, "b": 2}

    def test_non_consecutive_duplicates_are_not_repeats(self):
        s = ConsolidationPassStats("p", "m", "medium", 15, "u")
        s.record_turn([("a", {})])
        s.record_turn([("b", {})])
        s.record_turn([("a", {})])
        assert s.repeats == 0

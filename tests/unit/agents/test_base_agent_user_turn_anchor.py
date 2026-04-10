"""
Unit tests for BaseAgent._inject_user_turn_anchor.

Behavioral anchor (information-gap rule + posture rule) is prepended to the
LATEST user message only — historical user messages stay clean, model messages
stay clean, and the in-memory mutation never touches persisted state.
"""

import pytest

from src.agents.base_agent import (
    BaseAgent,
    USER_TURN_SYSTEM_ANCHOR,
)
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.ports.llm_port import Message, MessagePart


class _StubAgent(BaseAgent):
    """Minimal concrete BaseAgent so we can call instance methods."""

    async def can_handle(self, message: AgentMessage) -> bool:  # pragma: no cover
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def agent() -> _StubAgent:
    return _StubAgent(config=AgentConfig(agent_id="stub", agent_type="stub"))


class TestInjectUserTurnAnchor:
    def test_empty_history_returns_unchanged(self, agent):
        assert agent._inject_user_turn_anchor([]) == []

    def test_last_message_model_unchanged(self, agent):
        history = [
            Message(role="user", parts=[MessagePart(text="hello")]),
            Message(role="model", parts=[MessagePart(text="hi there")]),
        ]
        result = agent._inject_user_turn_anchor(history)

        # Same object structure — no anchor anywhere
        assert result == history
        assert USER_TURN_SYSTEM_ANCHOR not in result[0].parts[0].text
        assert USER_TURN_SYSTEM_ANCHOR not in result[1].parts[0].text

    def test_single_user_message_gets_anchor(self, agent):
        history = [Message(role="user", parts=[MessagePart(text="привет")])]
        result = agent._inject_user_turn_anchor(history)

        assert len(result) == 1
        text = result[0].parts[0].text
        assert text.startswith(USER_TURN_SYSTEM_ANCHOR)
        assert text.endswith("привет")
        # Separator between anchor and original text
        assert "\n\nпривет" in text

    def test_only_last_user_message_modified(self, agent):
        history = [
            Message(role="user", parts=[MessagePart(text="first user msg")]),
            Message(role="model", parts=[MessagePart(text="model reply")]),
            Message(role="user", parts=[MessagePart(text="second user msg")]),
        ]
        result = agent._inject_user_turn_anchor(history)

        # First user message untouched
        assert result[0].parts[0].text == "first user msg"
        # Model message untouched
        assert result[1].parts[0].text == "model reply"
        # Last user message has anchor
        assert result[2].parts[0].text.startswith(USER_TURN_SYSTEM_ANCHOR)
        assert result[2].parts[0].text.endswith("second user msg")

    def test_file_data_first_part_anchor_lands_on_text_part(self, agent):
        """If the first part is a file_data attachment, anchor must land on the
        next text part — not on the file part."""
        history = [
            Message(
                role="user",
                parts=[
                    MessagePart(file_data={"uri": "gs://bucket/file.pdf", "mime_type": "application/pdf"}),
                    MessagePart(text="please summarize this PDF"),
                ],
            )
        ]
        result = agent._inject_user_turn_anchor(history)

        assert result[0].parts[0].file_data is not None
        assert result[0].parts[0].text is None
        assert result[0].parts[1].text.startswith(USER_TURN_SYSTEM_ANCHOR)
        assert result[0].parts[1].text.endswith("please summarize this PDF")

    def test_anchor_does_not_mutate_input_history(self, agent):
        """Helper must return a new list — original history must stay clean
        so callers that retain the reference (e.g. for logging) see the
        unmodified state."""
        original_text = "original"
        original_part = MessagePart(text=original_text)
        original_msg = Message(role="user", parts=[original_part])
        history = [original_msg]

        agent._inject_user_turn_anchor(history)

        # Original message and part unchanged
        assert history[0] is original_msg
        assert history[0].parts[0] is original_part
        assert history[0].parts[0].text == original_text

    def test_anchor_preserves_other_part_fields(self, agent):
        """full_text, consolidation_text and other MessagePart fields must
        survive the rewrite."""
        history = [
            Message(
                role="user",
                parts=[
                    MessagePart(
                        text="stub",
                        full_text="full version of text",
                        consolidation_text="\n\nfact for memory",
                    )
                ],
            )
        ]
        result = agent._inject_user_turn_anchor(history)
        part = result[0].parts[0]

        assert part.text.startswith(USER_TURN_SYSTEM_ANCHOR)
        assert part.text.endswith("stub")
        assert part.full_text == "full version of text"
        assert part.consolidation_text == "\n\nfact for memory"

    def test_anchor_text_contains_both_load_bearing_phrases(self):
        """Smoke test: the constant must contain the two load-bearing phrases
        from each rule. If a future edit drops one, this test screams.

        Also guards against regressing to removed framings:
          - Named lenses (PSYCHIATRIST / PRISM / SCOUT / SCREENWRITER /
            CHIEF OF STAFF) — all caused real failure modes; see the history
            note block in base_agent.py for the lessons.
          - "warm reply" escape hatch — earlier SCREENWRITER had it; the LLM
            grabbed it on every conversational message and skipped tool calls.
        """
        # Rule 1 — information-gap rule
        assert "request for information the user does not yet have" in USER_TURN_SYSTEM_ANCHOR
        assert "Tone is not the test" in USER_TURN_SYSTEM_ANCHOR
        assert "Do not ignore your tools" in USER_TURN_SYSTEM_ANCHOR
        # Rule 2 — posture rule
        assert "Be proactive" in USER_TURN_SYSTEM_ANCHOR
        assert "Do not just mirror" in USER_TURN_SYSTEM_ANCHOR

        # Regression guards — removed framings must NOT come back
        assert "PSYCHIATRIST" not in USER_TURN_SYSTEM_ANCHOR
        assert "PRISM" not in USER_TURN_SYSTEM_ANCHOR
        assert "CHIEF OF STAFF" not in USER_TURN_SYSTEM_ANCHOR
        assert "warm reply" not in USER_TURN_SYSTEM_ANCHOR

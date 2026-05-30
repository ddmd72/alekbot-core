"""
Unit tests for the observability domain model (PromptContentRecord).

Pure domain: defaults, optionality, and that no field requires I/O.
"""

import time

from src.domain.observability import PromptContentRecord


class TestPromptContentRecord:
    def test_minimal_construction_has_safe_defaults(self):
        rec = PromptContentRecord()

        assert rec.trace_id is None
        assert rec.span_id is None
        assert rec.user_id is None
        assert rec.account_id is None
        assert rec.agent_id == ""
        assert rec.agent_type == ""
        assert rec.model == ""
        assert rec.provider is None
        assert rec.turn == 0
        assert rec.request_text is None
        assert rec.response_text is None
        assert rec.tool_calls is None
        assert rec.prompt_tokens == 0
        assert rec.completion_tokens == 0
        assert rec.total_tokens == 0
        assert rec.cache_read_tokens == 0
        assert rec.cache_creation_tokens == 0
        assert rec.latency_ms is None
        assert rec.error is None

    def test_timestamp_defaults_to_now(self):
        before = time.time()
        rec = PromptContentRecord()
        after = time.time()

        assert before <= rec.timestamp <= after

    def test_all_fields_round_trip(self):
        rec = PromptContentRecord(
            trace_id="tr_abc",
            span_id="sp_def",
            timestamp=1234.5,
            user_id="u1",
            account_id="a1",
            agent_id="smart_u1",
            agent_type="smart",
            model="claude-opus-4-8",
            provider="claude",
            turn=2,
            request_text="system + history",
            response_text="answer",
            tool_calls='[{"name": "x", "args": {}}]',
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cache_read_tokens=3,
            cache_creation_tokens=1,
            latency_ms=42.0,
            error=None,
        )

        dumped = rec.model_dump()
        assert dumped["trace_id"] == "tr_abc"
        assert dumped["model"] == "claude-opus-4-8"
        assert dumped["turn"] == 2
        assert dumped["total_tokens"] == 15
        assert dumped["latency_ms"] == 42.0

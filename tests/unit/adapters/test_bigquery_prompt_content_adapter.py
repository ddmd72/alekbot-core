"""
Wire tests for BigQueryPromptContentAdapter.

The adapter owns record building (request/response → row) and the background-task
set. record_turn schedules a fire-and-forget insert and returns immediately; the
write is best-effort (errors swallowed). Mock boundary: the google.cloud.bigquery
SDK (injected into sys.modules) — never the port.
"""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from src.adapters.bigquery_prompt_content_adapter import (
    BigQueryPromptContentAdapter,
    _PARTITION_EXPIRATION_MS,
    _render_messages,
)
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall, UsageMetadata


@pytest.fixture
def fake_bigquery(monkeypatch):
    """Inject a fake google.cloud.bigquery module; return (module, client)."""
    client = MagicMock()
    client.project = "proj"
    client.create_dataset = MagicMock()
    client.create_table = MagicMock()
    client.insert_rows_json = MagicMock(return_value=[])  # [] == success

    bq = MagicMock()
    bq.Client = MagicMock(return_value=client)
    bq.SchemaField = MagicMock(side_effect=lambda *a, **k: ("field", a, k))
    bq.Table = MagicMock(return_value=MagicMock())
    bq.TimePartitioning = MagicMock(return_value="PARTITION")
    bq.TimePartitioningType = MagicMock(DAY="DAY")

    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    return bq, client


@pytest.fixture
def adapter():
    return BigQueryPromptContentAdapter(dataset="ds", table="tbl", project="proj")


def _request() -> LLMRequest:
    return LLMRequest(
        model_name="claude-opus-4-8",
        system_instruction="you are a test",
        messages=[Message(role="user", parts=[MessagePart(text="hello")])],
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="answer",
        usage_metadata=UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


async def _drain(adapter) -> None:
    await asyncio.gather(*list(adapter._bg_tasks))


async def _record(adapter, request=None, response=None):
    await adapter.record_turn(
        request=request or _request(),
        response=response or _response(),
        agent_id="smart_u1",
        agent_type="smart",
        account_id="acct-1",
        turn=2,
        latency_ms=42.0,
        provider="claude",
    )
    await _drain(adapter)


class TestRecordTurn:
    async def test_creates_table_with_30day_ttl(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        await _record(adapter)

        client.create_dataset.assert_called_once_with("ds", exists_ok=True)
        client.create_table.assert_called_once()
        bq.TimePartitioning.assert_called_once()
        kwargs = bq.TimePartitioning.call_args.kwargs
        assert kwargs["field"] == "timestamp"
        assert kwargs["expiration_ms"] == _PARTITION_EXPIRATION_MS
        assert _PARTITION_EXPIRATION_MS == 30 * 24 * 60 * 60 * 1000

    async def test_inserts_row_built_from_request_and_response(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        await _record(adapter)

        client.insert_rows_json.assert_called_once()
        table_id, rows = client.insert_rows_json.call_args.args[:2]
        assert table_id == "proj.ds.tbl"
        row = rows[0]
        assert row["agent_id"] == "smart_u1"
        assert row["agent_type"] == "smart"
        assert row["model"] == "claude-opus-4-8"
        assert row["account_id"] == "acct-1"
        assert row["turn"] == 2
        assert row["provider"] == "claude"
        assert row["response_text"] == "answer"
        assert row["total_tokens"] == 15
        assert row["latency_ms"] == 42.0
        # request_text bundles system + history; timestamp is an ISO string
        assert "you are a test" in row["request_text"]
        assert "hello" in row["request_text"]
        assert isinstance(row["timestamp"], str)

    async def test_returns_immediately_without_blocking(self, adapter, fake_bigquery):
        # record_turn schedules a background task and returns before the insert runs.
        await adapter.record_turn(
            request=_request(), response=_response(), agent_id="a", agent_type="t",
            account_id=None, turn=0, latency_ms=1.0, provider="claude",
        )
        assert len(adapter._bg_tasks) == 1
        bq, client = fake_bigquery
        client.insert_rows_json.assert_not_called()  # not yet — still scheduled
        await _drain(adapter)
        client.insert_rows_json.assert_called_once()

    async def test_insert_errors_are_swallowed(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.return_value = [{"index": 0, "errors": ["bad"]}]
        await _record(adapter)  # must not raise

    async def test_client_exception_is_swallowed(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.side_effect = RuntimeError("BQ down")
        await _record(adapter)  # must not raise

    async def test_tool_calls_serialized(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        resp = LLMResponse(text=None, tool_calls=[ToolCall(name="search_memory", args={"q": "x"})])
        await _record(adapter, response=resp)

        row = client.insert_rows_json.call_args.args[1][0]
        assert "search_memory" in row["tool_calls"]


class TestRenderMessages:
    def test_renders_role_and_text(self):
        msgs = [
            Message(role="user", parts=[MessagePart(text="hi")]),
            Message(role="model", parts=[MessagePart(text="hello")]),
        ]
        out = _render_messages(msgs)
        assert "user: hi" in out
        assert "model: hello" in out


class TestRecordDrResult:
    async def test_durable_write_builds_deep_research_row(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        await adapter.record_dr_result(
            output_text="the report",
            query="research X",
            user_id="u1",
            account_id="acct-1",
            model="claude-opus-4-8",
            provider="claude",
            source="claude_job",
            job_id="job-9",
            pass_index=2,
            total_tokens=5000,
        )

        client.insert_rows_json.assert_called_once()
        row = client.insert_rows_json.call_args.args[1][0]
        assert row["agent_type"] == "deep_research"
        assert row["agent_id"] == "claude_job"
        assert row["provider"] == "claude"
        assert row["response_text"] == "the report"
        assert row["request_text"] == "research X"
        assert row["user_id"] == "u1"
        assert row["total_tokens"] == 5000
        assert row["turn"] == 2  # pass_index tagged via turn (no schema migration)

    async def test_single_pass_tags_turn_zero(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        await adapter.record_dr_result(
            output_text="r", query="q", user_id=None, account_id=None,
            model="m", provider="openai", source="openai_webhook",
        )
        assert client.insert_rows_json.call_args.args[1][0]["turn"] == 0

    async def test_durable_retries_then_logs_without_raising(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.return_value = [{"index": 0, "errors": ["transient"]}]
        adapter._DURABLE_ATTEMPTS = 3
        adapter._DURABLE_BACKOFF_S = 0  # no real sleeps in tests

        # Must not raise even though every attempt "fails".
        await adapter.record_dr_result(
            output_text="r", query="q", user_id=None, account_id=None,
            model="m", provider="claude", source="claude_job",
        )
        assert client.insert_rows_json.call_count == 3  # all attempts used

    async def test_durable_stops_on_first_success(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        adapter._DURABLE_BACKOFF_S = 0
        await adapter.record_dr_result(
            output_text="r", query="q", user_id=None, account_id=None,
            model="m", provider="claude", source="claude_job",
        )
        assert client.insert_rows_json.call_count == 1  # success → no retry

    async def test_durable_swallows_client_exception(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.side_effect = RuntimeError("BQ down")
        adapter._DURABLE_ATTEMPTS = 2
        adapter._DURABLE_BACKOFF_S = 0
        await adapter.record_dr_result(  # must not raise
            output_text="r", query="q", user_id=None, account_id=None,
            model="m", provider="claude", source="claude_job",
        )

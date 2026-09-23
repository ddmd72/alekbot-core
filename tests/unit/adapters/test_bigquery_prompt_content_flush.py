"""BigQueryPromptContentAdapter.flush — a request that ends must be able to wait for its
background writes. On Cloud Run the CPU is throttled once the response is sent, and writes
left pending past that point starved for minutes and then failed with SSL EOF
(2026-09-23, end-of-call voice turns)."""

import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.adapters.bigquery_prompt_content_adapter import BigQueryPromptContentAdapter
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart


@pytest.fixture
def client(monkeypatch):
    client = MagicMock()
    client.project = "proj"
    client.insert_rows_json = MagicMock(return_value=[])
    bq = MagicMock()
    bq.Client = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    return client


async def _record(adapter):
    await adapter.record_turn(
        request=LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])]),
        response=LLMResponse(text="yo"),
        agent_id="lelik_agent_u1", agent_type="lelik", account_id="a1",
        turn=0, latency_ms=1.0, provider="openai",
    )


@pytest.mark.asyncio
async def test_flush_waits_for_every_pending_write(client):
    adapter = BigQueryPromptContentAdapter(dataset="ds", table="tbl", project="proj")
    for _ in range(3):
        await _record(adapter)
    client.insert_rows_json.assert_not_called()

    await adapter.flush()

    assert client.insert_rows_json.call_count == 3
    assert not adapter._bg_tasks


@pytest.mark.asyncio
async def test_flush_with_nothing_pending_returns_at_once(client):
    adapter = BigQueryPromptContentAdapter(dataset="ds", table="tbl", project="proj")
    await adapter.flush()


@pytest.mark.asyncio
async def test_flush_is_bounded_and_does_not_cancel_a_slow_write(client):
    release = threading.Event()
    client.insert_rows_json.side_effect = lambda *a, **k: (release.wait(2), [])[1]
    adapter = BigQueryPromptContentAdapter(dataset="ds", table="tbl", project="proj")
    await _record(adapter)

    started = time.monotonic()
    await adapter.flush(timeout_s=0.05)

    assert time.monotonic() - started < 1.0
    [task] = list(adapter._bg_tasks)
    assert not task.cancelled()
    release.set()
    await task

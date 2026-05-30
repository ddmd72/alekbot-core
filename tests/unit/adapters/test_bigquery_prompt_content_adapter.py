"""
Wire tests for BigQueryPromptContentAdapter.

Mock boundary: the google.cloud.bigquery SDK (injected into sys.modules so the
adapter's lazy `from google.cloud import bigquery` binds to a fake). Never mock
at the port level — these tests verify the actual translation to SDK calls:
dataset/table creation, 30-day partition expiration, and the row insert.
"""

import sys
from unittest.mock import MagicMock

import pytest

from src.adapters.bigquery_prompt_content_adapter import (
    BigQueryPromptContentAdapter,
    _PARTITION_EXPIRATION_MS,
)
from src.domain.observability import PromptContentRecord


@pytest.fixture
def fake_bigquery(monkeypatch):
    """Inject a fake google.cloud.bigquery module and return (module, client)."""
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


def _record() -> PromptContentRecord:
    return PromptContentRecord(
        trace_id="tr_1",
        timestamp=1_700_000_000.0,
        agent_id="smart_u1",
        agent_type="smart",
        model="claude-opus-4-8",
        response_text="answer",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )


class TestStore:
    async def test_creates_dataset_and_table_with_30day_ttl(self, adapter, fake_bigquery):
        bq, client = fake_bigquery

        await adapter.store(_record())

        client.create_dataset.assert_called_once_with("ds", exists_ok=True)
        client.create_table.assert_called_once()
        # Partition expiration encodes the 30-day TTL.
        bq.TimePartitioning.assert_called_once()
        kwargs = bq.TimePartitioning.call_args.kwargs
        assert kwargs["field"] == "timestamp"
        assert kwargs["expiration_ms"] == _PARTITION_EXPIRATION_MS
        assert _PARTITION_EXPIRATION_MS == 30 * 24 * 60 * 60 * 1000

    async def test_inserts_row_with_iso_timestamp(self, adapter, fake_bigquery):
        bq, client = fake_bigquery

        await adapter.store(_record())

        client.insert_rows_json.assert_called_once()
        args = client.insert_rows_json.call_args.args
        table_id, rows = args[0], args[1]
        assert table_id == "proj.ds.tbl"
        assert len(rows) == 1
        row = rows[0]
        assert row["trace_id"] == "tr_1"
        assert row["model"] == "claude-opus-4-8"
        assert row["total_tokens"] == 15
        # timestamp serialized to ISO-8601 UTC string for the TIMESTAMP column
        assert isinstance(row["timestamp"], str)
        assert row["timestamp"].startswith("2023-11-")

    async def test_ensure_table_runs_only_once(self, adapter, fake_bigquery):
        bq, client = fake_bigquery

        await adapter.store(_record())
        await adapter.store(_record())

        assert client.create_table.call_count == 1
        assert client.insert_rows_json.call_count == 2

    async def test_insert_errors_are_swallowed(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.return_value = [{"index": 0, "errors": ["bad"]}]

        # Must not raise — best-effort contract.
        await adapter.store(_record())

    async def test_client_exception_is_swallowed(self, adapter, fake_bigquery):
        bq, client = fake_bigquery
        client.insert_rows_json.side_effect = RuntimeError("BQ down")

        # Must not raise — runs fire-and-forget on the request hot path.
        await adapter.store(_record())


class TestToRow:
    def test_timestamp_becomes_iso_utc_string(self):
        rec = PromptContentRecord(timestamp=1_700_000_000.0, agent_id="a")
        row = BigQueryPromptContentAdapter._to_row(rec)

        assert isinstance(row["timestamp"], str)
        assert "T" in row["timestamp"]  # ISO-8601
        assert row["timestamp"].endswith("+00:00")  # UTC

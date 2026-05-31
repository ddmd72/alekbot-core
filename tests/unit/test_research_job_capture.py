"""
Unit tests for job_main._capture_research_result.

The deep-research Cloud Run Job persists its (expensive) output to BigQuery
durably BEFORE delivery, capturing both passes — the first pass never reaches the
user but matters for history. No-op when BIGQUERY_PROMPT_DATASET is unset.
Mock boundary: the google.cloud.bigquery SDK (injected into sys.modules).
"""

import sys
from unittest.mock import MagicMock

import pytest

import job_main


@pytest.fixture
def fake_bigquery(monkeypatch):
    client = MagicMock()
    client.project = "proj"
    client.create_dataset = MagicMock()
    client.create_table = MagicMock()
    client.insert_rows_json = MagicMock(return_value=[])

    bq = MagicMock()
    bq.Client = MagicMock(return_value=client)
    bq.SchemaField = MagicMock(side_effect=lambda *a, **k: ("f", a, k))
    bq.Table = MagicMock(return_value=MagicMock())
    bq.TimePartitioning = MagicMock(return_value="P")
    bq.TimePartitioningType = MagicMock(DAY="DAY")

    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    return bq, client


_CONTEXT = {"account_id": "acct-1", "original_query": "q", "job_id": "job-1"}


async def test_noop_when_flag_off(monkeypatch, fake_bigquery):
    # DEBUG_PROMPTS is the global write switch; off → no capture even with a dataset.
    monkeypatch.setenv("DEBUG_PROMPTS", "false")
    monkeypatch.setenv("BIGQUERY_PROMPT_DATASET", "ds")
    bq, client = fake_bigquery

    await job_main._capture_research_result(
        {"text": "final", "round1_text": "r1", "second_pass": True}, "final", "u1", _CONTEXT
    )

    client.insert_rows_json.assert_not_called()


async def test_noop_when_dataset_unset(monkeypatch, fake_bigquery):
    monkeypatch.setenv("DEBUG_PROMPTS", "true")
    monkeypatch.delenv("BIGQUERY_PROMPT_DATASET", raising=False)
    bq, client = fake_bigquery

    await job_main._capture_research_result(
        {"text": "final", "round1_text": "r1", "second_pass": True}, "final", "u1", _CONTEXT
    )

    client.insert_rows_json.assert_not_called()


async def test_two_pass_captures_both(monkeypatch, fake_bigquery):
    monkeypatch.setenv("DEBUG_PROMPTS", "true")
    monkeypatch.setenv("BIGQUERY_PROMPT_DATASET", "ds")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    bq, client = fake_bigquery

    await job_main._capture_research_result(
        {"text": "final", "round1_text": "r1", "second_pass": True, "model": "m"},
        "final", "u1", _CONTEXT,
    )

    assert client.insert_rows_json.call_count == 2
    turns = sorted(call.args[1][0]["turn"] for call in client.insert_rows_json.call_args_list)
    assert turns == [1, 2]  # round-1 + critic/final


async def test_single_pass_captures_once(monkeypatch, fake_bigquery):
    monkeypatch.setenv("DEBUG_PROMPTS", "true")
    monkeypatch.setenv("BIGQUERY_PROMPT_DATASET", "ds")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    bq, client = fake_bigquery

    await job_main._capture_research_result(
        {"text": "final", "round1_text": "", "second_pass": False, "model": "m"},
        "final", "u1", _CONTEXT,
    )

    client.insert_rows_json.assert_called_once()
    assert client.insert_rows_json.call_args.args[1][0]["response_text"] == "final"

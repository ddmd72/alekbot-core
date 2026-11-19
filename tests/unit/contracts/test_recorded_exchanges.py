"""Every committed recording: its format and both contracts (P17).

The recordings (`tests/fixtures/providers/<provider>/<scenario>.json`, `recorded_exchanges.py`) are real exchanges
written by `make contract-record` (`scripts/contracts/record.py`). For each one:
1. the format: `_meta` names the provider, the operation, the scenario (one of the recorder's `SCENARIOS`), the source,
   the date, the outcome and the command that regenerates it; every scenario of the recorder (minus `UNREACHABLE`) has a
   recording;
2. every recorded request conforms to the request contract (`assert_request`; the recorder keeps the path, never a
   query: `test_recorded_exchanges_replay.py` shows the SDK sends none);
3. every recorded response conforms to the response contract (Anthropic: the event stream, `assert_stream`; OpenAI: the
   JSON body, `assert_response`);
4. no recorded request reaches a request-side `NOT_REACHABLE` entry that is still listed.
The replay through the real adapter and SDK is `test_recorded_exchanges_replay.py`.

The recordings settled what they were recorded for (TEST_COVERAGE_SYSTEMIC_RFC "Built (P17 recordings replay)"): the
response fields the contracts lacked are ALLOWED gaps, and so is P17-D1, the `caller: null` a replayed `server_tool_use`
block carries, which the provider answered 200. Its evidence stays pinned below; the meta-tests of the conformance files
refuse a gap no sample, shape or recording carries.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from scripts.contracts import record as rec
from scripts.contracts.extract_subset import PROVIDERS
from tests.contracts.openapi_contract import assert_request, assert_response, assert_stream
from tests.unit.contracts.claude_request_shapes import NOT_REACHABLE
from tests.unit.contracts.recorded_exchanges import EXCHANGES, RECORDINGS, provider_of, recording
from tests.unit.contracts.recorded_exchanges import recording_id as _id

META_KEYS = ("provider", "operation", "scenario", "source", "recorded_at", "regenerate", "outcome")

# P17-D1's ALLOWED gap: the null caller of a replayed block, narrowed to one block type.
(NULL_CALLER,) = [a for a in PROVIDERS["anthropic"].operations[0].allowed
                  if a.null and a.path == "messages[].content[].caller"]


def _null_caller_blocks(node: Any) -> list[dict]:
    """Every object in `node` that carries `caller: null`."""
    if isinstance(node, dict):
        own = [node] if "caller" in node and node["caller"] is None else []
        return own + [b for v in node.values() for b in _null_caller_blocks(v)]
    if isinstance(node, list):
        return [b for v in node for b in _null_caller_blocks(v)]
    return []


# ------------------------------------------------------------------------------------------------------------ format
def test_the_recordings_folder_is_not_empty():
    assert RECORDINGS


@pytest.mark.parametrize("path", RECORDINGS, ids=_id)
def test_every_recording_names_its_source_and_its_regeneration_command(path):
    doc = recording(path)
    meta = doc["_meta"]
    assert [k for k in META_KEYS if not meta.get(k)] == []
    assert (meta["provider"], meta["scenario"]) == (path.parent.name, path.stem)
    assert meta["provider"] in rec.SCENARIOS and meta["scenario"] in rec.SCENARIOS[meta["provider"]]()
    assert meta["source"] == "recorded"
    assert meta["operation"] == " ".join(rec.OPERATIONS[meta["provider"]])
    assert meta["regenerate"] == f"make contract-record PROVIDER={meta['provider']} ONLY={meta['scenario']}"
    _dt.datetime.strptime(meta["recorded_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert doc["exchanges"]


@pytest.mark.parametrize("provider", sorted(rec.SCENARIOS))
def test_every_scenario_of_the_recorder_has_a_recording(provider):
    wanted = set(rec.SCENARIOS[provider]()) - set(rec.UNREACHABLE[provider])
    assert {p.stem for p in RECORDINGS if p.parent.name == provider} == wanted


# --------------------------------------------------------------------------------------------- the request contract
def _request_problems(path: Path, n: int) -> None:
    request = recording(path)["exchanges"][n]["request"]
    assert_request(provider_of(path), request["method"], request["path"], request["headers"], request["body"], query={})


@pytest.mark.parametrize("path, n", [pytest.param(p, n, id=f"{_id(p)}#{n}") for p, n in EXCHANGES])
def test_every_recorded_request_conforms_to_the_request_contract(path, n):
    _request_problems(path, n)


def test_a_recorded_null_caller_is_on_the_allowed_block_type_and_was_answered_200_p17_d1():
    """Pins the evidence of P17-D1's ALLOWED gap: a recorded request carries `caller: null` only on the block type the
    gap is narrowed to, and the provider answered every such request 200 (it accepted the null)."""
    cases = [(p, n) for p, n in EXCHANGES if _null_caller_blocks(recording(p)["exchanges"][n]["request"]["body"])]
    assert cases, "no recording sends caller: null: the evidence of P17-D1's ALLOWED gap is gone"
    for path, n in cases:
        exchange = recording(path)["exchanges"][n]
        assert {b.get("type") for b in _null_caller_blocks(exchange["request"]["body"])} == {NULL_CALLER.variant}, (
            _id(path), n)
        assert exchange["response"]["status"] == 200, (_id(path), n)


# -------------------------------------------------------------------------------------------- the response contract
def _check_response(path: Path, n: int) -> None:
    provider = provider_of(path)
    request, response = recording(path)["exchanges"][n]["request"], recording(path)["exchanges"][n]["response"]
    if provider == "anthropic":
        assert_stream(provider, request["method"], request["path"], response["status"], response["events"])
    else:
        assert_response(provider, request["method"], request["path"], response["status"], response["body"])


@pytest.mark.parametrize("path, n", [pytest.param(p, n, id=f"{_id(p)}#{n}") for p, n in EXCHANGES])
def test_every_recorded_response_conforms_to_the_response_contract(path, n):
    _check_response(path, n)


def test_no_recorded_request_reaches_a_not_reachable_entry():
    """A `NOT_REACHABLE` entry of claude_request_shapes.py that a recording reaches leaves that list (its meta-test says
    how). `container`: the dynamic search recording did not pause (stop_reason end_turn), so no continuation was
    sent."""
    sent = {key for p in RECORDINGS if provider_of(p) == "anthropic"
            for ex in recording(p)["exchanges"] for key in ex["request"]["body"]}
    assert not set(NOT_REACHABLE) & sent

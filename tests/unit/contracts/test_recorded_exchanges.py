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

Open defects are strict xfails with their id (TEST_COVERAGE_SYSTEMIC_RFC "Built (P17 recordings replay)"): P17-D1 (a
replayed block carries `caller: null`, which the request contract lacks), P17-D2 and P17-D3 (the Anthropic and OpenAI
responses carry fields their response contracts lack). A pin per defect keeps a new violation from hiding behind one.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.contracts import record as rec
from tests.contracts.openapi_contract import assert_request, assert_response, assert_stream
from tests.unit.contracts.claude_request_shapes import NOT_REACHABLE
from tests.unit.contracts.recorded_exchanges import EXCHANGES, RECORDINGS, provider_of, recording
from tests.unit.contracts.recorded_exchanges import recording_id as _id

META_KEYS = ("provider", "operation", "scenario", "source", "recorded_at", "regenerate", "outcome")

_P17_D1 = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="P17-D1: a replayed tool block carries caller: null, absent from the request contract")
# The response fields the recordings carry and the response contracts lack (the violation, its event prefix removed).
RESPONSE_GAPS = {
    "anthropic": ("P17-D2", {"$.message.diagnostics: unknown field", "$.message.usage.speed: unknown field",
                             "$.content_block.content.abort_reason: unknown field"}),
    "openai": ("P17-D3", {"$.billing: unknown field", "$.frequency_penalty: unknown field",
                          "$.presence_penalty: unknown field", "$.store: unknown field"}),
}


def _has_null_caller(node: Any) -> bool:
    if isinstance(node, dict):
        return ("caller" in node and node["caller"] is None) or any(_has_null_caller(v) for v in node.values())
    return isinstance(node, list) and any(_has_null_caller(v) for v in node)


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


@pytest.mark.parametrize("path, n", [
    pytest.param(p, n, id=f"{_id(p)}#{n}",
                 marks=[_P17_D1] if _has_null_caller(recording(p)["exchanges"][n]["request"]["body"]) else [])
    for p, n in EXCHANGES])
def test_every_recorded_request_conforms_to_the_request_contract(path, n):
    _request_problems(path, n)


def test_a_null_caller_is_the_only_violation_of_a_recorded_request_p17_d1():
    """Pins P17-D1's evidence: the requests that carry `caller: null` break the contract by it alone, and the provider
    answered them 200 (it accepted the null)."""
    cases = [(p, n) for p, n in EXCHANGES if _has_null_caller(recording(p)["exchanges"][n]["request"]["body"])]
    for path, n in cases:
        with pytest.raises(AssertionError) as failure:
            _request_problems(path, n)
        lines = str(failure.value).splitlines()
        assert lines[0].endswith("breaks the contract:") and lines[1:]
        assert all(re.fullmatch(r"\$\..*\.caller: wrong type .*", line) for line in lines[1:]), lines
        assert recording(path)["exchanges"][n]["response"]["status"] == 200


# -------------------------------------------------------------------------------------------- the response contract
def _check_response(path: Path, n: int) -> None:
    provider = provider_of(path)
    request, response = recording(path)["exchanges"][n]["request"], recording(path)["exchanges"][n]["response"]
    if provider == "anthropic":
        assert_stream(provider, request["method"], request["path"], response["status"], response["events"])
    else:
        assert_response(provider, request["method"], request["path"], response["status"], response["body"])


def _gap_mark(path: Path) -> pytest.MarkDecorator:
    defect, gaps = RESPONSE_GAPS[provider_of(path)]
    return pytest.mark.xfail(
        strict=True, raises=AssertionError,
        reason=f"{defect}: the recorded response carries fields the contract lacks: {sorted(gaps)}")


@pytest.mark.parametrize("path, n", [pytest.param(p, n, id=f"{_id(p)}#{n}", marks=[_gap_mark(p)])
                                     for p, n in EXCHANGES])
def test_every_recorded_response_conforms_to_the_response_contract(path, n):
    _check_response(path, n)


@pytest.mark.parametrize("path, n", [pytest.param(p, n, id=f"{_id(p)}#{n}") for p, n in EXCHANGES])
def test_a_recorded_response_breaks_the_contract_only_by_the_known_gaps_p17_d2_d3(path, n):
    try:
        _check_response(path, n)
        return
    except AssertionError as failure:
        lines = str(failure).splitlines()
    assert lines[0].endswith("breaks the contract:"), lines[0]
    found = {re.sub(r"^event\[\d+\] ", "", line) for line in lines[1:]}
    assert found <= RESPONSE_GAPS[provider_of(path)][1], sorted(found - RESPONSE_GAPS[provider_of(path)][1])


def test_no_recorded_request_reaches_a_not_reachable_entry():
    """A `NOT_REACHABLE` entry of claude_request_shapes.py that a recording reaches leaves that list (its meta-test says
    how). `container`: the dynamic search recording did not pause (stop_reason end_turn), so no continuation was
    sent."""
    sent = {key for p in RECORDINGS if provider_of(p) == "anthropic"
            for ex in recording(p)["exchanges"] for key in ex["request"]["body"]}
    assert not set(NOT_REACHABLE) & sent

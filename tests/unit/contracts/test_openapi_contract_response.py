"""tests/contracts/openapi_contract.py: a response is checked against a provider contract, every violation named.

Over an invented mini contract (tests/fixtures/openapi_contract/sprockets/openapi.yaml): a JSON response and a
server-sent-event stream (one schema per event's data).
"""
from pathlib import Path

import pytest

from tests.contracts import openapi_contract as oc

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openapi_contract"
BODY = {"id": "sp-1", "teeth": 12, "finish": None, "items": [{"type": "cog", "size": 3}, {"type": "spacer"}],
        "usage": {"input_tokens": 5, "details": {"cached_tokens": 0}}}


def _violations(check, *args, **kwargs) -> list[str]:
    """The violations `check` (`assert_response` or `assert_stream`) lists ([] when it passes). Any other failure (an
    operation not found, no contract, no response for the status or media type) propagates: read as [], it would make
    a test asserting [] pass vacuously."""
    try:
        check(*args, contracts_dir=FIXTURES, **kwargs)
    except AssertionError as failure:
        head, *violations = str(failure).splitlines()
        if not head.endswith(" breaks the contract:") or not violations:
            raise
        return violations
    return []


def _problems(body=None, status=200, media="application/json", path="/v1/sprockets") -> list[str]:
    return _violations(oc.assert_response, "sprockets", "POST", path, status, BODY if body is None else body,
                       media=media)


def _stream_problems(events) -> list[str]:
    return _violations(oc.assert_stream, "sprockets", "POST", "/v1/sprockets", 200, events)


def test_a_conforming_response_passes():
    assert _problems() == []


def test_the_status_may_be_a_string_and_the_url_path_carries_the_base_path():
    assert _problems(status="200", path="/v1/sprockets") == []


def test_a_status_or_media_type_absent_from_the_contract_fails():
    with pytest.raises(AssertionError, match=r"no 404 application/json response in the contract"):
        oc.assert_response("sprockets", "post", "/sprockets", 404, BODY, contracts_dir=FIXTURES)
    with pytest.raises(AssertionError, match=r"no 200 text/plain response"):
        oc.assert_response("sprockets", "post", "/sprockets", 200, BODY, media="text/plain", contracts_dir=FIXTURES)


def test_unknown_field_at_the_top_level():
    assert _problems({**BODY, "weight": 3}) == ["$.weight: unknown field"]


def test_unknown_field_at_depth():
    body = {**BODY, "items": [{"type": "cog", "size": 3, "pitch": 1}],
            "usage": {"input_tokens": 5, "details": {"cached_tokens": 0, "written": 1}}}
    assert _problems(body) == ["$.items[0].pitch: unknown field", "$.usage.details.written: unknown field"]


def test_wrong_type_at_the_top_level_and_at_depth():
    body = {**BODY, "teeth": "twelve", "usage": {"input_tokens": 5, "details": {"cached_tokens": "none"}}}
    assert _problems(body) == ["$.teeth: wrong type (expected integer)",
                               "$.usage.details.cached_tokens: wrong type (expected integer)"]


def test_a_variant_is_judged_by_the_type_it_names():
    body = {**BODY, "items": [{"type": "spacer", "width": "wide"}, {"type": "washer"}]}
    assert _problems(body) == ["$.items[0].width: wrong type (expected number)",
                               "$.items[1].type: value not allowed ('washer')"]


def test_a_nullable_enum_reports_the_value():
    assert _problems({**BODY, "finish": "chrome"}) == ["$.finish: value not allowed ('chrome')"]


def test_missing_required():
    body = {k: v for k, v in BODY.items() if k != "teeth"}
    assert _problems(body) == ["$.teeth: missing required"]


def test_an_unread_field_takes_any_value_but_stays_required():
    assert _problems({**BODY, "id": {"anything": [1, 2]}}) == []
    assert _problems({k: v for k, v in BODY.items() if k != "id"}) == ["$.id: missing required"]


def test_a_deprecated_field_is_accepted_in_a_response():
    assert _problems({**BODY, "legacy_grade": "a"}) == []


def test_one_event_is_checked_against_the_stream_schema():
    assert _problems({"type": "delta", "text": "hi"}, media="text/event-stream") == []
    assert _problems({"type": "delta", "text": 5}, media="text/event-stream") == [
        "$.text: wrong type (expected string)"]


def test_a_whole_stream_names_the_event_of_each_violation():
    events = [
        {"event": "start", "data": {"type": "start", "sprocket": {**BODY, "extra": 1}}},
        {"event": "ping", "data": {"type": "ping"}},
        {"event": "delta", "data": {"type": "delta", "text": "a"}},
        {"event": "delta", "data": {"type": "delta", "text": 2}},
        {"event": "stop", "data": {"type": "stop"}},
    ]
    assert _stream_problems(events) == [
        "event[0] $.sprocket.extra: unknown field",
        "event[3] $.text: wrong type (expected string)",
        "event[4] $.type: value not allowed ('stop')",
    ]


def test_an_event_name_that_differs_from_its_data_type_fails():
    events = [{"event": "start", "data": {"type": "delta", "text": "a"}}]
    assert _stream_problems(events) == ["event[0]: event name 'start' differs from data.type 'delta'"]


def test_a_conforming_stream_passes():
    events = [{"event": "start", "data": {"type": "start", "sprocket": BODY}},
              {"event": "delta", "data": {"type": "delta", "text": "a"}}]
    assert _stream_problems(events) == []


def test_the_response_validator_is_compiled_once():
    _problems()
    op = oc._operation(FIXTURES / "sprockets" / "openapi.yaml", "post", "/sprockets")
    compiled = op._responses[("200", "application/json")]
    _problems({**BODY, "teeth": 1})
    assert op._responses[("200", "application/json")] is compiled

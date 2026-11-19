"""tests/contracts/openapi_contract.py: a contract's `x-alek-aliases` rewrite the request path before the lookup, and an
operation whose body is not JSON takes bytes unvalidated.

Over an invented mini contract (tests/fixtures/openapi_contract/lockers/openapi.yaml). Paths are the decoded wire paths
with the server's base path, queries passed explicitly.
"""
from pathlib import Path
from urllib.parse import quote

import pytest
from yarl import URL

from tests.contracts import openapi_contract as oc

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openapi_contract"


def _problems(method: str, path: str, body=None) -> list[str]:
    try:
        oc.assert_request("lockers", method, path, {}, body, query={}, raw_path=_raw(path), contracts_dir=FIXTURES)
    except AssertionError as failure:
        return str(failure).splitlines()[1:]
    return []


def _raw(path: str) -> str:
    """The path as aiohttp sends it (yarl's requoting), still percent-encoded."""
    return URL("https://api.lockers.example.com" + quote(path, safe="/:'()&")).raw_path


def _operation(method: str, path: str) -> str:
    return oc.operation_path("lockers", method, path, raw_path=_raw(path), contracts_dir=FIXTURES)


def _alias(path: str) -> str | None:
    return oc.applied_alias("lockers", path, raw_path=_raw(path), contracts_dir=FIXTURES)


# ------------------------------------------------------------------------------------------------------- aliases
def test_an_aliased_path_is_looked_up_as_its_target():
    assert _problems("GET", "/v1.0/me/locker/items/item-1") == []
    assert _operation("GET", "/v1.0/me/locker/items/item-1") == "/lockers/{locker-id}/items/{item-id}"
    assert _alias("/v1.0/me/locker/items/item-1") == "/me/locker/**"


def test_colon_addressing_resolves_to_the_item_and_to_an_operation_under_it():
    assert _operation("GET", "/v1.0/me/locker/items/item-1:/Tom's plan.docx:") == "/lockers/{locker-id}/items/{item-id}"
    assert _alias("/v1.0/me/locker/items/item-1:/Tom's plan.docx:") == "/me/locker/items/{item-id}:/{path}:"
    path = "/v1.0/me/locker/items/item-1:/Tom's plan.docx:/content"
    assert _operation("PUT", path) == "/lockers/{locker-id}/items/{item-id}/content"
    assert _alias(path) == "/me/locker/items/{item-id}:/{path}:/**"


def test_the_first_matching_rule_wins():
    """The catch-all `/me/locker/**` matches a colon path too; ordered first, the colon rule is the one applied."""
    assert _alias("/v1.0/me/locker/items/item-1:/a.pdf:/content") != "/me/locker/**"


def test_the_path_parameters_are_validated_after_aliasing():
    assert _problems("GET", "/v1.0/me/locker/items/box-1") == ["path item-id: value not allowed (pattern '^item-')"]


def test_a_target_name_the_pattern_does_not_capture_is_filled_and_an_absent_target_fails():
    with pytest.raises(AssertionError,
                       match=r"GET /v1.0/me/locker/items/item-1/permissions \(aliased to "
                             r"/lockers/\(alias\)/items/item-1/permissions\) is not in"):
        _operation("GET", "/v1.0/me/locker/items/item-1/permissions")


def test_a_path_no_rule_matches_is_looked_up_as_it_is():
    assert _alias("/v1.0/lockers/l-1/items/item-1") is None
    assert _operation("GET", "/v1.0/lockers/l-1/items/item-1") == "/lockers/{locker-id}/items/{item-id}"


def test_a_contract_without_aliases_matches_as_before():
    assert oc.applied_alias("widgets", "/v2/widgets", raw_path=None, contracts_dir=FIXTURES) is None


def test_an_aliased_body_is_still_validated():
    assert _problems("PATCH", "/v1.0/me/locker/items/item-1", {"name": "R&D"}) == []
    assert _problems("PATCH", "/v1.0/me/locker/items/item-1", {"name": "R&D", "size": 3}) == ["$.size: unknown field"]


# --------------------------------------------------------------------------------------------------- a raw body
@pytest.mark.parametrize("body", [b"%PDF-1.7", bytearray(b"%PDF-1.7"), "plain text"])
def test_a_raw_body_takes_bytes_or_text_unvalidated(body):
    assert _problems("PUT", "/v1.0/lockers/l-1/items/item-1/content", body) == []


@pytest.mark.parametrize("body", [{"name": "birth-certificate.pdf"}, [1, 2]])
def test_a_json_body_sent_to_a_raw_body_operation_fails(body):
    assert _problems("PUT", "/v1.0/lockers/l-1/items/item-1/content", body) == [
        "$: wrong type (the operation takes a application/octet-stream body, not JSON)"]


def test_a_required_raw_body_that_is_missing_fails():
    assert _problems("PUT", "/v1.0/lockers/l-1/items/item-1/content") == [
        "$: missing required (a application/octet-stream body)"]


# --------------------------------------------------------------------------------------- the raw path of an alias
def test_a_contract_with_aliases_needs_the_raw_path():
    with pytest.raises(TypeError, match="raw_path: this contract has aliases"):
        oc.assert_request("lockers", "GET", "/v1.0/me/locker/items/item-1", {}, None, query={}, contracts_dir=FIXTURES)


def test_a_contract_without_aliases_refuses_a_raw_path():
    with pytest.raises(TypeError, match="raw_path: only a contract with aliases"):
        oc.operation_path("widgets", "POST", "/v2/widgets", raw_path="/v2/widgets", contracts_dir=FIXTURES)


def test_the_raw_path_must_decode_to_the_path():
    with pytest.raises(ValueError, match="does not decode to path"):
        oc.operation_path("lockers", "GET", "/v1.0/me/locker/items/item-1", raw_path="/v1.0/me/locker/items/item-2",
                          contracts_dir=FIXTURES)


@pytest.mark.parametrize("path", [
    "/v1.0/me/locker/items/item-1:item-2:/a.pdf:/content",   # a bare colon inside the captured id
    "/v1.0/me/locker/items/item-1:/a:b.pdf:/content",        # a bare colon inside the captured name
])
def test_a_captured_value_never_holds_a_bare_colon(path):
    assert _alias(path) != "/me/locker/items/{item-id}:/{path}:/**"


def test_an_encoded_slash_stays_inside_its_segment_and_the_parameters_are_decoded_after_the_match():
    decoded = "/v1.0/me/locker/items/item-1:/R&D/Tom's plan.docx:/content"
    raw = "/v1.0/me/locker/items/item-1:/R&D%2FTom's%20plan.docx:/content"
    assert oc.applied_alias("lockers", decoded, raw_path=raw, contracts_dir=FIXTURES) == \
        "/me/locker/items/{item-id}:/{path}:/**"
    assert oc.operation_path("lockers", "PUT", decoded, raw_path=raw, contracts_dir=FIXTURES) == \
        "/lockers/{locker-id}/items/{item-id}/content"
    oc.assert_request("lockers", "PUT", "/v1.0/lockers/l-1/items/item-1/content", {}, b"%PDF-", query={},
                      raw_path="/v1.0/lockers/l-1/items/item%2D1/content", contracts_dir=FIXTURES)   # item%2D1: item-1

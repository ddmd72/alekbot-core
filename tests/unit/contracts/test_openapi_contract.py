"""tests/contracts/openapi_contract.py: a request is checked against a provider contract, every violation named.

Over an invented mini contract (tests/fixtures/openapi_contract/widgets/openapi.yaml).
"""
from pathlib import Path

import pytest

from tests.contracts import openapi_contract as oc

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openapi_contract"
HEADERS = {"X-Widget-Tenant": "t-1"}
BODY = {"name": "w", "count": 2, "color": "red", "parts": [{"type": "bolt", "length": 1.5}, {"text": "n"}]}


def _problems(body=None, headers=None, path="/widgets") -> list[str]:
    try:
        oc.assert_request("widgets", "POST", path, HEADERS if headers is None else headers,
                          BODY if body is None else body, contracts_dir=FIXTURES)
    except AssertionError as failure:
        return str(failure).splitlines()[1:]
    return []


def test_a_conforming_request_passes():
    assert _problems() == []


def test_the_url_path_with_the_server_base_path_is_the_operation():
    assert _problems(path="/v2/widgets") == []


def test_an_operation_absent_from_the_contract_fails():
    with pytest.raises(AssertionError, match="GET /widgets is not in"):
        oc.assert_request("widgets", "get", "/widgets", HEADERS, BODY, contracts_dir=FIXTURES)


def test_a_provider_without_a_contract_fails():
    with pytest.raises(AssertionError, match="no contract at"):
        oc.assert_request("gadgets", "post", "/widgets", HEADERS, BODY, contracts_dir=FIXTURES)


def test_unknown_field():
    assert _problems({**BODY, "weight": 3}) == ["$.weight: unknown field"]


def test_wrong_type():
    assert _problems({**BODY, "count": "two"}) == ["$.count: wrong type (expected integer)"]


def test_missing_required():
    body = {k: v for k, v in BODY.items() if k != "name"}
    assert _problems(body) == ["$.name: missing required"]


def test_value_not_allowed():
    assert _problems({**BODY, "color": "green"}) == ["$.color: value not allowed ('green')"]


def test_deprecated_field_fails_unless_the_contract_allows_it():
    assert _problems({**BODY, "legacy_size": 3}) == ["$.legacy_size: deprecated"]
    assert _problems({**BODY, "old_mode": "x"}) == []


def test_a_variant_is_judged_by_the_type_it_names():
    body = {**BODY, "parts": [{"type": "gear", "teeth": "many"}, {"type": "bolt", "length": 1, "head": "hex"}]}
    assert _problems(body) == ["$.parts[0].teeth: wrong type (expected integer)", "$.parts[1].head: unknown field"]


def test_a_variant_type_no_variant_has_is_named():
    assert _problems({**BODY, "parts": [{"type": "washer"}]}) == ["$.parts[0].type: value not allowed ('washer')"]


def test_an_untyped_item_is_judged_by_the_variant_that_does_not_require_a_type():
    assert _problems({**BODY, "parts": [{"text": 5}]}) == ["$.parts[0].text: wrong type (expected string)"]


def test_a_union_of_scalars_reports_the_wrong_type():
    assert _problems({**BODY, "label": 7}) == ["$.label: wrong type (expected one of ['null', 'string'])"]


def test_every_violation_is_listed_sorted():
    body = {"count": "two", "weight": 1, "legacy_size": 3}
    assert _problems(body) == [
        "$.count: wrong type (expected integer)", "$.legacy_size: deprecated", "$.name: missing required",
        "$.weight: unknown field",
    ]


def test_a_comma_list_header_is_checked_item_by_item():
    headers = {**HEADERS, "x-widget-beta": "beta-a, beta-legacy,beta-z"}
    assert _problems(headers=headers) == ["header x-widget-beta[2]: value not allowed ('beta-z')"]


def test_a_required_header_is_missing():
    assert _problems(headers={}) == ["header x-widget-tenant: missing required"]


def test_undeclared_headers_are_not_checked():
    assert _problems(headers={**HEADERS, "User-Agent": "sdk/1", "x-api-key": "k"}) == []


def test_the_validator_is_compiled_once_per_operation():
    _problems()
    before = oc._operation.cache_info()
    _problems({**BODY, "count": 5})
    after = oc._operation.cache_info()
    assert after.misses == before.misses and after.hits == before.hits + 1


# ------------------------------------------------------------------------------- path and query parameters (parcels)
def _query_problems(query, path="/v1/parcels", method="GET", body=None) -> list[str]:
    try:
        oc.assert_request("parcels", method, path, {}, body, query=query, contracts_dir=FIXTURES)
    except AssertionError as failure:
        return str(failure).splitlines()[1:]
    return []


def test_a_conforming_query_passes_with_its_values_read_as_their_types():
    query = {"limit": ["5"], "status": ["open"], "tag": ["a", "b"], "fields": ["id,weight"], "express": ["true"],
             "ratio": ["0.5"]}
    assert _query_problems(query) == []


def test_an_unknown_query_parameter_is_named():
    assert _query_problems({"limit": ["5"], "zz_unknown": ["1"]}) == ["query zz_unknown: unknown query parameter"]


def test_a_required_query_parameter_is_missing():
    assert _query_problems({"status": ["open"]}) == ["query limit: missing required"]
    assert _query_problems(None) == ["query limit: missing required"]


def test_query_values_that_do_not_read_as_the_type_are_wrong_types():
    query = {"limit": ["five"], "express": ["yes"], "ratio": ["half"]}
    assert _query_problems(query) == ["query express: wrong type (expected boolean)",
                                      "query limit: wrong type (expected integer)",
                                      "query ratio: wrong type (expected number)"]


def test_a_query_enum_and_a_bound_are_checked():
    assert _query_problems({"limit": ["0"], "status": ["lost"]}) == [
        "query limit: value not allowed (minimum 1)", "query status: value not allowed ('lost')"]


def test_a_repeated_scalar_query_parameter_fails():
    assert _query_problems({"limit": ["5", "6"]}) == ["query limit: wrong type (expected one value, got 2)"]


def test_a_repeated_parameter_is_checked_item_by_item_and_explode_false_splits_on_commas():
    assert _query_problems({"limit": ["5"], "fields": ["id,size"]}) == ["query fields[1]: value not allowed ('size')"]
    assert _query_problems({"limit": ["5"], "fields": ["id", "size"]}) == ["query fields[1]: value not allowed ('size')"]


def test_a_query_value_passed_as_one_string_is_a_caller_error():
    with pytest.raises(TypeError, match="list of strings"):
        _query_problems({"limit": "5"})


def test_a_templated_path_is_matched_and_its_parameters_validated():
    assert _query_problems(None, path="/v1/parcels/7") == []
    assert _query_problems(None, path="/v1/parcels/seven") == ["path parcelId: wrong type (expected integer)"]


def test_a_template_inside_a_segment_is_matched():
    assert _query_problems(None, path="/v1/parcels/7/labels/front.pdf") == []
    assert _query_problems(None, path="/v1/parcels/7/labels/side.pdf") == ["path labelId: value not allowed ('side')"]
    with pytest.raises(AssertionError, match="is not in"):
        oc.operation_path("parcels", "get", "/v1/parcels/7/labels/front.png", contracts_dir=FIXTURES)


def test_a_literal_path_wins_over_a_template():
    assert oc.operation_path("parcels", "GET", "/v1/parcels/tracked", contracts_dir=FIXTURES) == "/parcels/tracked"
    assert oc.operation_path("parcels", "GET", "/v1/parcels/7", contracts_dir=FIXTURES) == "/parcels/{parcelId}"


def test_the_template_with_more_literal_segments_wins_and_a_tie_fails():
    path = oc.operation_path("parcels", "get", "/v1/depots/d-1/parcels", contracts_dir=FIXTURES)
    assert path == "/depots/{depotId}/parcels"
    with pytest.raises(AssertionError, match="matches several operations"):
        oc.operation_path("parcels", "get", "/v1/routes/east", contracts_dir=FIXTURES)


def test_a_path_with_an_extra_or_empty_segment_matches_no_template():
    for path in ("/v1/parcels/7/extra", "/v1/parcels/"):
        with pytest.raises(AssertionError, match="is not in"):
            oc.operation_path("parcels", "get", path, contracts_dir=FIXTURES)


def test_a_percent_encoded_path_segment_is_decoded_before_validation():
    assert _query_problems(None, path="/v1/parcels/7/labels/fr%6Fnt.pdf") == []


def test_a_body_sent_to_an_operation_without_one_fails():
    assert _query_problems(None, path="/v1/parcels/tracked", body={"weight": 1}) == [
        "$: unknown field (the operation takes no request body)"]


def test_a_body_operation_still_validates_its_body():
    assert _query_problems(None, method="POST", body={"weight": "x"}) == ["$.weight: wrong type (expected integer)"]


def test_one_validator_serves_every_concrete_path_of_a_template():
    _query_problems(None, path="/v1/parcels/1")
    before = oc._operation.cache_info()
    _query_problems(None, path="/v1/parcels/2")
    after = oc._operation.cache_info()
    assert after.misses == before.misses and after.hits == before.hits + 1


# --------------------------------------------------------------------------- no vacuous pass (reviewer probes p1-p3)
def _contract_at(tmp_path: Path, server: str, paths: dict) -> Path:
    """A one-off invented contract `tmp/<provider "tmp">/openapi.yaml`; returns the contracts directory."""
    import yaml

    (tmp_path / "tmp").mkdir()
    doc = {"openapi": "3.1.0", "info": {"title": "tmp", "version": "1"}, "servers": [{"url": server}],
           "components": {"schemas": {}}, "paths": paths}
    (tmp_path / "tmp" / "openapi.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


def test_the_query_keyword_is_required_and_none_is_refused():
    with pytest.raises(TypeError, match="query"):
        oc.assert_request("parcels", "GET", "/v1/parcels/7", {}, None, contracts_dir=FIXTURES)
    with pytest.raises(TypeError, match=r"pass \{\} when the request carries no query"):
        oc.assert_request("parcels", "GET", "/v1/parcels/7", {}, None, query=None, contracts_dir=FIXTURES)


@pytest.mark.parametrize("path", ["/v1/parcels/7?zz_unknown=1", "/v1/parcels/7#top", "/v1/parcels?limit=5"])
def test_a_query_or_fragment_left_in_the_path_is_refused(path):
    with pytest.raises(ValueError, match="pass the URL path alone"):
        oc.assert_request("parcels", "GET", path, {}, None, query={"limit": ["5"]}, contracts_dir=FIXTURES)
    with pytest.raises(ValueError, match="pass the URL path alone"):
        oc.operation_path("parcels", "GET", path, contracts_dir=FIXTURES)


@pytest.mark.parametrize("path", ["/parcels/7", "/parcels/tracked", "/v10/parcels/7", "/v1parcels/7"])
def test_the_server_base_path_is_required_when_the_server_has_one(path):
    with pytest.raises(AssertionError, match="is not in"):
        oc.operation_path("parcels", "GET", path, contracts_dir=FIXTURES)


def test_a_server_without_a_base_path_takes_the_path_as_sent(tmp_path):
    root = _contract_at(tmp_path, "https://api.example.com", {"/parcels": {"get": {}}})
    oc.assert_request("tmp", "GET", "/parcels", {}, None, query={}, contracts_dir=root)
    with pytest.raises(AssertionError, match="is not in"):
        oc.assert_request("tmp", "GET", "/v1/parcels", {}, None, query={}, contracts_dir=root)


def test_the_contract_form_names_an_operation_by_its_paths_key_only_when_asked():
    oc.assert_request("parcels", "GET", "/parcels/tracked", {}, None, query={}, contract_form=True,
                      contracts_dir=FIXTURES)
    with pytest.raises(AssertionError, match="is not in"):
        oc.assert_request("parcels", "GET", "/parcels/tracked", {}, None, query={}, contracts_dir=FIXTURES)
    with pytest.raises(AssertionError, match="is not in"):
        oc.assert_request("parcels", "GET", "/v1/parcels/tracked", {}, None, query={}, contract_form=True,
                          contracts_dir=FIXTURES)


@pytest.mark.parametrize("value", ["٥", "５", "+5", "05", " 5"])
def test_an_integer_value_must_be_canonical_ascii_digits(value):
    with pytest.raises(AssertionError, match=r"query limit: wrong type \(expected integer\)"):
        oc.assert_request("parcels", "GET", "/v1/parcels", {}, None, query={"limit": [value]}, contracts_dir=FIXTURES)


@pytest.mark.parametrize("value", ["٠.5", "+0.5", "00.5", ".5"])
def test_a_number_value_must_be_canonical_ascii_digits(value):
    with pytest.raises(AssertionError, match=r"query ratio: wrong type \(expected number\)"):
        oc.assert_request("parcels", "GET", "/v1/parcels", {}, None, query={"limit": ["5"], "ratio": [value]},
                          contracts_dir=FIXTURES)


def _deprecated_paths() -> dict:
    return {"/items/{kind}": {"get": {"parameters": [
        {"in": "path", "name": "kind", "required": True, "deprecated": True, "schema": {"type": "string"}},
        {"in": "query", "name": "$top", "required": False, "deprecated": True, "schema": {"type": "integer"}},
        {"in": "query", "name": "$skip", "required": False, "deprecated": True, "x-alek-extension": "still sent (2026-11-19)",
         "schema": {"type": "integer"}},
    ]}}}


def test_a_deprecated_parameter_fails_when_sent_unless_the_contract_allows_it(tmp_path):
    root = _contract_at(tmp_path, "https://api.example.com/v1.0", _deprecated_paths())
    with pytest.raises(AssertionError) as failure:
        oc.assert_request("tmp", "GET", "/v1.0/items/full", {}, None, query={"$top": ["5"], "$skip": ["5"]},
                          contracts_dir=root)
    assert str(failure.value).splitlines()[1:] == ["path kind: deprecated", "query $top: deprecated"]


def test_a_path_segment_is_not_decoded_a_second_time():
    """`path` is already the decoded URL path: `%6F` in it is the three characters, not an `o`."""
    with pytest.raises(AssertionError, match=r"path labelId: value not allowed \('fr%6Fnt'\)"):
        oc.assert_request("parcels", "GET", "/v1/parcels/7/labels/fr%6Fnt.pdf", {}, None, query={},
                          contracts_dir=FIXTURES)

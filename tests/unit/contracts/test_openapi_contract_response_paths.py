"""tests/contracts/openapi_contract.py: a response check finds its operation exactly as `assert_request` does (P17).

`assert_response` and `assert_stream` take the path of the request the response answers: the decoded URL path with the
server's base path, matched through the same lookup as a request, and `raw_path` for a contract with aliases (required
there, refused anywhere else). The contract's own `paths` key is not a shortcut: without the base path it names no
request. Over the invented contracts of tests/fixtures/openapi_contract/ (sprockets: server `/v1`; lockers: aliases).
"""
from pathlib import Path

import pytest
import yaml

from tests.contracts import openapi_contract as oc

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openapi_contract"
SPROCKET = {"id": "sp-1", "teeth": 12, "items": [{"type": "spacer"}]}
STREAM = [{"event": "delta", "data": {"type": "delta", "text": "hi"}}]
LOCKER_ITEM = "/lockers/{locker-id}/items/{item-id}"


def test_the_wire_path_with_the_base_path_resolves_for_both_checks():
    oc.assert_response("sprockets", "POST", "/v1/sprockets", 200, SPROCKET, contracts_dir=FIXTURES)
    oc.assert_stream("sprockets", "POST", "/v1/sprockets", 200, STREAM, contracts_dir=FIXTURES)


def test_the_contracts_own_paths_key_without_the_base_path_fails_the_response_check():
    with pytest.raises(AssertionError, match=r"POST /sprockets is not in"):
        oc.assert_response("sprockets", "POST", "/sprockets", 200, SPROCKET, contracts_dir=FIXTURES)


def test_the_contracts_own_paths_key_without_the_base_path_fails_the_stream_check():
    with pytest.raises(AssertionError, match=r"POST /sprockets is not in"):
        oc.assert_stream("sprockets", "POST", "/sprockets", 200, STREAM, contracts_dir=FIXTURES)


def test_the_openai_operation_is_found_only_under_its_v1_server():
    body = {"output": [], "status": "completed"}
    with pytest.raises(AssertionError, match=r"POST /responses is not in"):
        oc.assert_response("openai", "POST", "/responses", 200, body)
    with pytest.raises(AssertionError, match=r"POST /responses is not in"):
        oc.assert_stream("openai", "POST", "/responses", 200, [{"event": None, "data": body}])


@pytest.mark.parametrize("check, payload", [(oc.assert_response, SPROCKET), (oc.assert_stream, STREAM)])
def test_a_raw_path_is_refused_by_a_contract_without_aliases(check, payload):
    with pytest.raises(TypeError, match="only a contract with aliases"):
        check("sprockets", "POST", "/v1/sprockets", 200, payload, raw_path="/v1/sprockets", contracts_dir=FIXTURES)


@pytest.fixture
def lockers_with_a_response(tmp_path) -> Path:
    """The aliased lockers contract with a 200 JSON response on its item read (a copy under tmp_path)."""
    doc = yaml.safe_load((FIXTURES / "lockers" / "openapi.yaml").read_text(encoding="utf-8"))
    doc["paths"][LOCKER_ITEM]["get"]["responses"] = {"200": {"description": "the item", "content": {
        "application/json": {"schema": {"type": "object", "additionalProperties": False,
                                        "properties": {"name": {"type": "string"}}}}}}}
    (tmp_path / "lockers").mkdir()
    (tmp_path / "lockers" / "openapi.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return tmp_path


def test_a_contract_with_aliases_requires_the_raw_path(lockers_with_a_response):
    with pytest.raises(TypeError, match="this contract has aliases"):
        oc.assert_response("lockers", "GET", "/v1.0/me/locker/items/item-1", 200, {"name": "Invoice FA-0137"},
                           contracts_dir=lockers_with_a_response)


def test_a_contract_with_aliases_resolves_the_raw_path_through_its_aliases(lockers_with_a_response):
    path = "/v1.0/me/locker/items/item-1"
    oc.assert_response("lockers", "GET", path, 200, {"name": "Invoice FA-0137"}, raw_path=path,
                       contracts_dir=lockers_with_a_response)
    with pytest.raises(AssertionError, match=r"\$\.size: unknown field"):
        oc.assert_response("lockers", "GET", path, 200, {"size": 3}, raw_path=path,
                           contracts_dir=lockers_with_a_response)

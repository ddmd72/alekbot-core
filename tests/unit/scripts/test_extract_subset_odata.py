"""scripts/contracts/extract_subset.py, what Microsoft Graph needs: the OData transforms of an OpenAPI 3.0 source, a body
that is not JSON, a query parameter the source lacks (Allowed), aliases, and one component name per pruning.

Offline: the source is an invented OData-shaped fragment (tests/fixtures/extract_subset/lockers_openapi.yaml).
"""
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.contracts import extract_subset as es
from scripts.contracts import spec_drift as sd

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "lockers_openapi.yaml"
REF = "#/components/schemas/"
ITEM = "/lockers/{locker-id}/items/{item-id}"

PATCH = es.Operation(method="patch", path=ITEM, fields=("label", "name", "shade", "size", "tags"))
CHILDREN = es.Operation(method="post", path=ITEM + "/children", fields=("name",))
CONTENT = es.Operation(method="put", path=ITEM + "/content", fields=(), raw_body="application/octet-stream")
GET = es.Operation(method="get", path=ITEM, fields=())
ALIASES = (
    es.Alias("/me/locker/items/{item-id}:/{path}:/**", "/lockers/{locker-id}/items/{item-id}/**", "colon addressing"),
    es.Alias("/me/locker/**", "/lockers/{locker-id}/**", "the user's own locker"),
)
LOCKERS = es.Provider(name="lockers", repo="example/lockers-openapi", spec_file="openapi.yaml", odata=True,
                      operations=(PATCH, CHILDREN, CONTENT, GET), aliases=ALIASES)


def _source() -> dict:
    return sd.parse(FIXTURE.read_bytes())


def _odata() -> dict:
    return es.odata_document(_source(), LOCKERS.operations)


def _build(*ops: es.Operation, aliases: tuple = ALIASES) -> dict:
    provider = replace(LOCKERS, operations=ops or LOCKERS.operations, aliases=aliases)
    return es.build_subset(_odata(), provider, source="https://example.com/lockers@abc", version="v1.0",
                           retrieved="2026-11-19", transforms=es._TRANSFORMS + es._ODATA_TRANSFORMS)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _allow(**kw) -> es.Allowed:
    return es.Allowed(reason=kw.pop("reason", "our adapter sends it (locker_adapter.py)"), date="2026-11-19", **kw)


# -------------------------------------------------------------------------------------------- the OData transforms
def test_discriminators_and_generator_annotations_are_dropped_but_a_property_named_like_one_is_kept():
    spec = _odata()
    keys = {k for node in _walk(spec) if isinstance(node, dict) for k in node}
    assert "discriminator" not in keys
    assert not {k for k in keys if k.startswith("x-ms-")} - {"x-ms-note"}
    assert "x-ms-note" in spec["components"]["schemas"]["ex.label"]["properties"]


def test_odata_type_is_no_longer_required():
    spec = _odata()
    required = [node["required"] for node in _walk(spec) if isinstance(node, dict) and isinstance(node.get("required"), list)]
    assert all("@odata.type" not in names for names in required)   # every list held only it: none is left
    assert "required" not in spec["components"]["schemas"]["ex.label"]


def test_nullable_becomes_a_null_type_and_the_null_member_of_a_reference_becomes_null():
    props = _odata()["components"]["schemas"]["ex.item"]["allOf"][1]["properties"]
    assert props["name"] == {"type": ["string", "null"]}
    assert props["shade"] == {"type": ["string", "null"], "enum": ["light", "dark", None]}
    assert props["label"]["anyOf"] == [{"$ref": REF + "ex.label"}, {"type": "null"}]
    assert props["tags"]["items"] == {"type": ["string", "null"]}
    assert not any("nullable" in node for node in _walk(_odata()) if isinstance(node, dict))


@pytest.mark.parametrize("node", [
    {"$ref": REF + "ex.label", "nullable": True},
    {"anyOf": [{"type": "string"}], "nullable": True},
    {"nullable": True},
])
def test_a_nullable_the_transform_cannot_express_fails_loudly(node):
    spec = {"paths": {}, "components": {"schemas": {"ex.odd": copy.deepcopy(node)}}}
    with pytest.raises(ValueError, match="nullable"):
        es.odata_document(spec, ())


def test_the_subset_is_flattened_pruned_and_closed_after_the_transforms():
    schemas = _build()["components"]["schemas"]
    item = schemas["ex.item"]
    assert sorted(item["properties"]) == sorted(PATCH.fields) and item["additionalProperties"] is False
    assert "required" not in item
    label = schemas["ex.label"]
    assert label["additionalProperties"] is False and sorted(label["properties"]) == ["@odata.type", "text", "x-ms-note"]


def test_the_odata_transforms_are_named_and_the_source_is_pinned():
    fetched = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return FIXTURE.read_bytes()

    doc = es.openapi_subset(LOCKERS, "0123abc", "2026-11-19", fetch=fetch)
    assert fetched == ["https://raw.githubusercontent.com/example/lockers-openapi/0123abc/openapi.yaml"]
    assert doc["openapi"] == "3.1.0" and doc["servers"] == [{"url": "https://api.lockers.example.com/v1.0"}]
    for transform in (*es._ODATA_TRANSFORMS, es._RAW_BODY_TRANSFORM, es._ALIAS_TRANSFORM, es._PARAMETER_TRANSFORM):
        assert transform in doc["x-transforms"]


# ------------------------------------------------------------------------------------------ one name per pruning
def test_a_component_pruned_differently_by_two_operations_gets_two_names():
    doc = _build()
    assert doc["paths"][ITEM]["patch"]["requestBody"]["content"]["application/json"]["schema"] == {"$ref": REF + "ex.item"}
    children = doc["paths"][ITEM + "/children"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert children == {"$ref": REF + "ex.item_2"}
    assert sorted(doc["components"]["schemas"]["ex.item_2"]["properties"]) == ["name"]


def test_a_component_pruned_the_same_way_is_kept_once():
    twin = replace(CHILDREN, fields=PATCH.fields)
    doc = _build(PATCH, twin)
    assert "ex.item_2" not in doc["components"]["schemas"]
    assert doc["paths"][ITEM + "/children"]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": REF + "ex.item"}


# ---------------------------------------------------------------------------------------------------- a raw body
def test_a_raw_body_is_kept_with_its_media_type_and_schema():
    body = _build()["paths"][ITEM + "/content"]["put"]["requestBody"]
    assert body == {"required": True,
                    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}


def test_the_raw_body_transform_is_named_only_when_an_operation_has_one():
    assert es._RAW_BODY_TRANSFORM in _build()["x-transforms"]
    assert es._RAW_BODY_TRANSFORM not in _build(PATCH, GET, aliases=())["x-transforms"]


@pytest.mark.parametrize("op, message", [
    (replace(CONTENT, fields=("name",)), "body fields and a raw body are exclusive"),
    (replace(CONTENT, raw_body="text/plain"), "no text/plain request body"),
    (replace(CONTENT, allowed=(_allow(path="", field="name", schema={"type": "string"}),)),
     "body paths on an operation with a raw body"),
])
def test_a_raw_body_entry_the_source_does_not_match_fails_loudly(op, message):
    with pytest.raises(ValueError, match=message):
        _build(PATCH, op, aliases=())


# ------------------------------------------------------------------------------------- an allowed query parameter
def test_an_allowed_query_parameter_is_injected_with_its_reason():
    op = replace(GET, query=("$select",), allowed=(_allow(path="query", field="select", schema={"type": "string"}),))
    params = _build(op, aliases=())["paths"][ITEM]["get"]["parameters"]
    assert [(p["in"], p["name"]) for p in params] == [
        ("path", "item-id"), ("path", "locker-id"), ("query", "$select"), ("query", "select")]
    assert params[-1] == {"name": "select", "in": "query", "required": False, "schema": {"type": "string"},
                          "x-alek-extension": "our adapter sends it (locker_adapter.py) (2026-11-19)"}


@pytest.mark.parametrize("allowed, message", [
    (_allow(path="query", field="$select", schema={"type": "string"}), "stale ALLOWED query parameter '\\$select'"),
    (_allow(path="query", field="select"), "a query parameter needs a field \\(its name\\) and a schema"),
    (_allow(path="query", value="select"), "a query parameter needs a field"),
])
def test_an_allowed_query_parameter_without_its_parts_or_stale_is_refused(allowed, message):
    with pytest.raises(ValueError, match=message):
        _build(replace(GET, allowed=(allowed,)), aliases=())


def test_an_allowed_query_parameter_is_not_a_body_path():
    op = replace(CONTENT, allowed=(_allow(path="query", field="mode", schema={"type": "string"}),))
    params = _build(op, aliases=())["paths"][ITEM + "/content"]["put"]["parameters"]
    assert ("query", "mode") in {(p["in"], p["name"]) for p in params}


# ------------------------------------------------------------------------------------------------------- aliases
def test_the_aliases_are_written_in_order_with_their_reasons():
    assert _build()["x-alek-aliases"] == [
        {"pattern": a.pattern, "target": a.target, "reason": a.reason} for a in ALIASES]


def test_a_provider_without_aliases_writes_none_and_names_no_alias_transform():
    doc = _build(PATCH, aliases=())
    assert "x-alek-aliases" not in doc and es._ALIAS_TRANSFORM not in doc["x-transforms"]


@pytest.mark.parametrize("alias, message", [
    (es.Alias("/me/locker/items/{item-id}/thumbnails", ITEM + "/thumbnails", "absent"),
     "is not a kept operation path"),
    (es.Alias("/me/archive/**", "/archives/{archive-id}/**", "absent"), "no kept operation under it"),
    (es.Alias("/me/locker/**", ITEM, "half a rest"), "both or neither end in /\\*\\*"),
    (es.Alias("/me/locker/items/{item-id}", ITEM, " "), "without a reason"),
])
def test_an_alias_that_names_no_kept_operation_or_lacks_a_reason_is_refused(alias, message):
    with pytest.raises(ValueError, match=message):
        _build(aliases=(alias,))


def test_the_graph_table_aliases_every_drive_path_to_a_kept_operation():
    graph = es.PROVIDERS["graph"]
    kept = {op.path for op in graph.operations}
    for alias in graph.aliases:
        target = alias.target
        assert (any(p.startswith(target[:-2]) for p in kept) if target.endswith("/**") else target in kept), alias

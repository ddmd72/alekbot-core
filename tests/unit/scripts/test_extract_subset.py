"""scripts/contracts/extract_subset.py: build a provider's request-contract subset from its spec or its SDK types.

Offline: the OpenAPI source is an invented fragment (tests/fixtures/extract_subset/), the SDK source invented TypedDicts
below; nothing is fetched.
"""
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Literal, Union

import pydantic
import pytest
import yaml
from anthropic._utils import PropertyInfo
from typing_extensions import Annotated, Required, TypedDict

from scripts.contracts import extract_subset as es
from scripts.contracts import spec_drift as sd

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "widgets_openapi.yaml"
REF = "#/components/schemas/"

OP = es.Operation(
    method="post", path="/widgets",
    fields=("model", "name", "description", "color", "legacy_size", "parts", "shape", "layout", "since"),
    variants={"parts[]": ("bolt", "gear"), "shape": ("circle",)},
    open=("layout",),
)
WIDGETS = es.Provider(name="widgets", repo="example/widgets-openapi", spec_file="openapi.yaml", operations=(OP,))


def _spec() -> dict:
    return sd.parse(FIXTURE.read_bytes())


def _build(op: es.Operation = OP, spec: dict | None = None) -> dict:
    provider = replace(WIDGETS, operations=(op,))
    return es.build_subset(spec or _spec(), provider, source="https://example.com/widgets@abc", version="9.9.9",
                           retrieved="2026-11-18")


def _schemas(doc: dict) -> dict:
    return doc["components"]["schemas"]


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _allow(**kw) -> es.Allowed:
    return es.Allowed(reason=kw.pop("reason", "our adapter sends it (widget_adapter.py)"), date="2026-11-18", **kw)


# ------------------------------------------------------------------------------------------------------ the document
def test_the_document_carries_source_retrieval_date_and_transforms():
    doc = _build()
    assert doc["openapi"] == "3.1.0"
    assert doc["x-source"] == "https://example.com/widgets@abc" and doc["x-retrieved"] == "2026-11-18"
    assert doc["x-transforms"] == list(es._TRANSFORMS)
    assert doc["servers"] == [{"url": "https://api.widgets.example.com/v2"}]
    op = doc["paths"]["/widgets"]["post"]
    assert op["operationId"] == "createWidget"
    assert op["requestBody"]["content"]["application/json"]["schema"] == {"$ref": REF + "CreateWidget"}


def test_prune_keeps_only_the_listed_top_level_fields():
    body = _schemas(_build())["CreateWidget"]
    assert sorted(body["properties"]) == sorted(OP.fields)
    assert "internal_note" not in body["properties"]


def test_prune_keeps_only_the_listed_variants_and_drops_unreached_components():
    schemas = _schemas(_build())
    parts = schemas["CreateWidget"]["properties"]["parts"]["items"]
    assert parts == {"anyOf": [{"$ref": REF + "Bolt"}, {"$ref": REF + "Gear"}]}
    assert schemas["CreateWidget"]["properties"]["shape"] == {"$ref": REF + "Circle"}  # one variant: no union left
    assert not {"Spring", "Square", "Part", "WidgetBase"} & set(schemas)


def test_allof_of_objects_is_flattened_into_one_object():
    body = _schemas(_build())["CreateWidget"]
    assert "allOf" not in body and body["type"] == "object"
    assert "model" in body["properties"] and body["required"] == ["name"]


def test_oneof_becomes_anyof_everywhere():
    assert not any(isinstance(n, dict) and "oneOf" in n for n in _walk(_build()))


def test_every_object_with_properties_is_closed_except_an_open_path():
    schemas = _schemas(_build())
    for name, schema in schemas.items():
        if "properties" in schema:
            assert schema["additionalProperties"] is False, name
    assert schemas["CreateWidget"]["properties"]["layout"] == {"type": "object"}  # open: our own JSON Schema


def _schema_nodes(node):
    """Schema nodes only: a `properties` map's keys are field names, not keywords."""
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            children = value.values() if key == "properties" else [value]
            for child in children:
                yield from _schema_nodes(child)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_nodes(value)


def test_documentation_keys_are_dropped_but_a_property_named_like_one_is_kept():
    doc = _build()
    assert _schemas(doc)["CreateWidget"]["properties"]["description"] == {"type": "string"}
    for schema in _schemas(doc).values():
        for node in _schema_nodes(schema):
            assert not {"description", "summary", "example", "x-oaiTypeLabel"} & set(node), node


def test_yaml_dates_become_iso_strings():
    since = _schemas(_build())["CreateWidget"]["properties"]["since"]
    assert since == {"default": "2026-01-15", "format": "date", "type": "string"}


def test_a_literal_list_next_to_its_plain_type_is_dropped():
    assert _schemas(_build())["CreateWidget"]["properties"]["model"] == {"type": "string"}


def test_a_shared_component_keeps_its_source_name():
    assert _schemas(_build())["Color"] == {"enum": ["blue", "red"], "type": "string"}


# ------------------------------------------------------------------------------------------------------ determinism
def test_rendering_is_byte_identical_across_runs_and_source_key_order():
    first = es.render(_build())
    spec = _spec()
    spec["components"]["schemas"] = dict(reversed(list(spec["components"]["schemas"].items())))
    body = spec["components"]["schemas"]["CreateWidget"]["allOf"][1]
    body["properties"] = dict(reversed(list(body["properties"].items())))
    assert es.render(_build()) == first
    assert es.render(_build(spec=spec)) == first


def test_write_refuses_a_subset_over_the_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "MAX_BYTES", 500)
    with pytest.raises(ValueError, match="over the 500 cap"):
        es.write("widgets", _build(), tmp_path)
    assert not (tmp_path / "widgets").exists()


def test_write_puts_the_subset_under_the_provider(tmp_path):
    path = es.write("widgets", _build(), tmp_path)
    assert path == tmp_path / "widgets" / "openapi.yaml"
    assert yaml.safe_load(path.read_text()) == yaml.safe_load(es.render(_build()))


# --------------------------------------------------------------------------------------------- the table's guards
@pytest.mark.parametrize("op, message", [
    (replace(OP, fields=tuple(f for f in OP.fields if f != "name")), "required body fields not listed"),
    (replace(OP, fields=OP.fields + ("weight",)), "listed fields absent from the source"),
    (replace(OP, variants={**OP.variants, "parts[]": ("bolt", "nut")}), r"variants absent at parts\[\]: \['nut'\]"),
    (replace(OP, variants={"partz[]": ("bolt",)}), "under no listed field"),
    (replace(OP, open=("layout", "color.rows")), "never reached"),
    (replace(OP, open=()), "recursive schema Layout at layout"),
    (replace(OP, variants={**OP.variants, "shape": ("square_ish",)}), "no wanted variant at a union of shape"),
])
def test_a_table_entry_the_source_does_not_match_fails_loudly(op, message):
    with pytest.raises(ValueError, match=message):
        _build(op)


# ------------------------------------------------------------------------------------------------------ ALLOWED
def test_allowed_variant_value_is_injected_as_a_closed_tagged_variant():
    op = replace(OP, allowed=(_allow(path="parts[]", value="washer"),))
    parts = _schemas(_build(op))["CreateWidget"]["properties"]["parts"]["items"]["anyOf"]
    assert parts[-1] == {
        "type": "object", "properties": {"type": {"const": "washer"}}, "required": ["type"],
        "additionalProperties": False, "x-alek-extension": "our adapter sends it (widget_adapter.py) (2026-11-18)",
    }


def test_allowed_scalar_value_is_injected_next_to_the_source_values():
    op = replace(OP, allowed=(_allow(path="color", value="green"),))
    color = _schemas(_build(op))["CreateWidget"]["properties"]["color"]
    assert color == {"anyOf": [{"$ref": REF + "Color"},
                               {"const": "green", "x-alek-extension": "our adapter sends it (widget_adapter.py) "
                                                                      "(2026-11-18)"}]}


def test_allowed_field_is_added_to_the_object():
    op = replace(OP, allowed=(_allow(path="", field="tenant", schema={"type": "string"}),))
    tenant = _schemas(_build(op))["CreateWidget"]["properties"]["tenant"]
    assert tenant["type"] == "string" and tenant["x-alek-extension"].startswith("our adapter sends it")


def test_allowed_deprecated_field_keeps_the_mark_and_gains_the_extension():
    op = replace(OP, allowed=(_allow(path="", field="legacy_size"),))
    size = _schemas(_build(op))["CreateWidget"]["properties"]["legacy_size"]
    assert size["deprecated"] is True and size["x-alek-extension"].startswith("our adapter sends it")


def test_a_deprecated_field_not_allowed_keeps_only_the_mark():
    size = _schemas(_build())["CreateWidget"]["properties"]["legacy_size"]
    assert size == {"deprecated": True, "type": "integer"}


@pytest.mark.parametrize("allowed, message", [
    (_allow(path="parts[]", value="spring"), "stale ALLOWED at parts\\[\\]: the source now has variant 'spring'"),
    (_allow(path="color", value="red"), "stale ALLOWED at color: the source now accepts 'red'"),
    (_allow(path="", field="name", schema={"type": "string"}), "stale ALLOWED at <body>: the source now has field"),
    (_allow(path="", field="name"), "'name' is no longer deprecated"),
    (_allow(path="", field="weight"), "deprecated field 'weight' is absent"),
])
def test_the_extractor_refuses_to_write_a_stale_allowed_entry(allowed, message):
    with pytest.raises(ValueError, match=message):
        _build(replace(OP, allowed=(allowed,)))


@pytest.mark.parametrize("allowed, message", [
    (_allow(path="color", reason=" ", value="green"), "without a reason"),
    (_allow(path="color"), "exactly one of value / field"),
    (_allow(path="header:x-widget-beta", value="b"), "not a declared header"),
])
def test_an_allowed_entry_without_its_parts_is_refused(allowed, message):
    with pytest.raises(ValueError, match=message):
        _build(replace(OP, allowed=(allowed,)))


# ------------------------------------------------------------------------------------------------------ SDK source
class GadgetKnob(TypedDict, total=False):
    type: Required[Literal["knob"]]
    turns: int


class GadgetLever(TypedDict, total=False):
    type: Required[Literal["lever"]]


class GadgetKnobModel(pydantic.BaseModel):
    """An SDK response model the request union also accepts."""

    type: Literal["knob"]
    turns: int


class GadgetParams(TypedDict, total=False):
    name: Required[str]
    parts: Iterable[Union[GadgetKnob, GadgetLever, GadgetKnobModel]]
    tenant_id: Annotated[str, PropertyInfo(alias="x-gadget-tenant")]


class GadgetSize(TypedDict, total=False):
    size_mm: Annotated[int, PropertyInfo(alias="sizeMm")]


class GadgetParamsRenamed(TypedDict, total=False):
    name: Required[str]
    size: GadgetSize


GadgetBeta = Union[str, Literal["beta-b-2026-01-01", "beta-a-2026-01-01"]]

_HERE = "tests.unit.scripts.test_extract_subset"
SDK_OP = es.Operation(
    method="post", path="/v1/gadgets", sdk_body=f"{_HERE}:GadgetParams",
    fields=("name", "parts"), variants={"parts[]": ("knob",)},
    headers={"x-gadget-beta": f"{_HERE}:GadgetBeta"}, header_aliases={"tenant_id": "x-gadget-tenant"},
)
GADGETS = es.Provider(name="gadgets", sdk="anthropic", server="https://api.gadgets.example.com", operations=(SDK_OP,))


def _sdk_build(op: es.Operation = SDK_OP) -> dict:
    provider = replace(GADGETS, operations=(op,))
    return es.build_subset(es.sdk_document(op, "1.2.3"), provider, source="gadgets-sdk@1.2.3", version="1.2.3",
                           retrieved="2026-11-18")


def test_sdk_header_alias_field_is_not_a_body_field():
    source = es.sdk_document(SDK_OP, "1.2.3")
    assert "tenant_id" not in source["components"]["schemas"]["GadgetParams"]["properties"]
    assert set(_sdk_build()["components"]["schemas"]["GadgetParams"]["properties"]) == {"name", "parts"}


def test_sdk_alias_the_table_does_not_know_fails_loudly():
    op = replace(SDK_OP, sdk_body=f"{_HERE}:GadgetParamsRenamed", fields=("name", "size"), variants={})
    with pytest.raises(ValueError, match="GadgetSize.size_mm has an alias 'sizeMm'"):
        es.sdk_document(op, "1.2.3")


def test_sdk_response_models_are_dropped_from_unions():
    doc = _sdk_build()
    schemas = doc["components"]["schemas"]
    assert schemas["GadgetParams"]["properties"]["parts"]["items"] == {"$ref": REF + "GadgetKnob"}
    assert "GadgetKnobModel" not in schemas and "GadgetLever" not in schemas
    assert schemas["GadgetKnob"]["additionalProperties"] is False


def test_sdk_header_is_a_comma_list_of_the_literals_plus_allowed_values():
    op = replace(SDK_OP, allowed=(_allow(path="header:x-gadget-beta", value="beta-c-2026-02-01"),))
    (param,) = _sdk_build(op)["paths"]["/v1/gadgets"]["post"]["parameters"]
    assert param["name"] == "x-gadget-beta" and param["in"] == "header" and param["style"] == "simple"
    assert param["schema"]["items"]["anyOf"] == [
        {"type": "string", "enum": ["beta-a-2026-01-01", "beta-b-2026-01-01"]},
        {"const": "beta-c-2026-02-01", "x-alek-extension": "our adapter sends it (widget_adapter.py) (2026-11-18)"},
    ]


def test_sdk_header_allowed_value_the_sdk_now_has_is_stale():
    op = replace(SDK_OP, allowed=(_allow(path="header:x-gadget-beta", value="beta-a-2026-01-01"),))
    with pytest.raises(ValueError, match="stale ALLOWED at header:x-gadget-beta"):
        es.sdk_document(op, "1.2.3")


def test_sdk_document_names_the_server_and_renders_deterministically():
    doc = _sdk_build()
    assert doc["servers"] == [{"url": "https://api.gadgets.example.com"}]
    assert es.render(doc) == es.render(_sdk_build())


def test_the_cli_refuses_a_ref_for_an_sdk_source():
    with pytest.raises(SystemExit, match="--ref applies to an OpenAPI source"):
        es.main(["--provider", "anthropic", "--ref", "abc"])


def test_the_openapi_source_url_is_pinned_by_the_ref():
    fetched = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return FIXTURE.read_bytes()

    doc = es.openapi_subset(WIDGETS, "0123abc", "2026-11-18", fetch=fetch)
    assert fetched == ["https://raw.githubusercontent.com/example/widgets-openapi/0123abc/openapi.yaml"]
    assert doc["x-source"] == fetched[0] and doc["info"]["version"] == "9.9.9"


# ------------------------------------------------------------------------------------- path and query parameters
PARCELS_OPENAPI = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "parcels_openapi.yaml"
PARCELS_DISCOVERY = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "parcels_discovery.json"
GET_PARCEL = es.Operation(method="get", path="/parcels/{parcelId}", fields=(), query=("$top", "fields"))


def _parcels(*ops: es.Operation) -> dict:
    provider = es.Provider(name="parcels", repo="example/parcels-openapi", spec_file="openapi.yaml", operations=ops)
    return es.build_subset(sd.parse(PARCELS_OPENAPI.read_bytes()), provider, source="https://example.com/parcels@abc",
                           version="2.0.0", retrieved="2026-11-19")


def _params(doc: dict, path: str, method: str = "get") -> dict:
    return {(p["in"], p["name"]): p for p in doc["paths"][path][method].get("parameters") or []}


def test_an_operation_without_fields_has_no_request_body():
    op = _parcels(GET_PARCEL)["paths"]["/parcels/{parcelId}"]["get"]
    assert "requestBody" not in op and op["operationId"] == "getParcel"


def test_path_parameters_are_all_kept_and_query_parameters_only_when_listed():
    params = _params(_parcels(GET_PARCEL), "/parcels/{parcelId}")
    assert sorted(params) == [("path", "parcelId"), ("query", "$top"), ("query", "fields")]
    assert params[("path", "parcelId")] == {"name": "parcelId", "in": "path", "required": True,
                                            "schema": {"type": "integer"}}


def test_a_parameter_ref_and_a_schema_ref_are_inlined_and_documentation_dropped():
    params = _params(_parcels(GET_PARCEL), "/parcels/{parcelId}")
    assert params[("query", "$top")] == {"name": "$top", "in": "query", "schema": {"type": "integer", "minimum": 0}}
    assert params[("query", "fields")] == {
        "name": "fields", "in": "query", "style": "form", "explode": False,
        "schema": {"type": "array", "items": {"type": "string", "enum": ["id", "weight"]}}}


def test_the_parameter_transform_is_named_only_when_parameters_are_kept():
    assert es._PARAMETER_TRANSFORM in _parcels(GET_PARCEL)["x-transforms"]
    assert es._PARAMETER_TRANSFORM not in _build()["x-transforms"]


def test_a_listed_header_of_a_bodyless_operation_is_kept_as_written():
    params = _params(_parcels(replace(GET_PARCEL, headers={"x-trace": "unused"})), "/parcels/{parcelId}")
    assert params[("header", "x-trace")] == {"name": "x-trace", "in": "header", "schema": {"type": "string"}}


@pytest.mark.parametrize("op, message", [
    (replace(GET_PARCEL, query=("$top", "page")), r"listed query parameters absent from the source: \['page'\]"),
    (es.Operation(method="get", path="/parcels/{parcelId}/labels/{labelId}", fields=()),
     r"path parameters \['parcelId'\] do not match the template"),
    (es.Operation(method="put", path="/parcels/{parcelId}", fields=()), "the source requires a request body"),
    (replace(GET_PARCEL, variants={"parts[]": ("bolt",)}), "body paths on an operation without body fields"),
])
def test_a_parameter_entry_the_source_does_not_match_fails_loudly(op, message):
    with pytest.raises(ValueError, match=message):
        _parcels(op)


# ------------------------------------------------------------------------------------------------- discovery source
LIST_PARCELS = es.Operation(method="get", path="/depots/{depotId}/parcels", fields=(),
                            query=("limit", "tag", "express", "status"))
PARCELS_D = es.Provider(name="parcels", repo="example/discovery", spec_file="discoveries/parcels.v1.json",
                        discovery=True, operations=(LIST_PARCELS,))


def _discovery() -> dict:
    return sd.parse(PARCELS_DISCOVERY.read_bytes())


def _from_discovery(*ops: es.Operation) -> dict:
    provider = replace(PARCELS_D, operations=ops or (LIST_PARCELS,))
    spec = es.discovery_document(_discovery(), provider.operations)
    return es.build_subset(spec, provider, source="https://example.com/parcels@abc", version="v1-20261101",
                           retrieved="2026-11-19", transforms=es._TRANSFORMS + es._DISCOVERY_TRANSFORMS)


def test_discovery_kept_methods_become_operations_under_root_url_and_service_path():
    doc = _from_discovery()
    assert doc["servers"] == [{"url": "https://parcels.example.com/parcels/v1"}]
    assert list(doc["paths"]) == ["/depots/{depotId}/parcels"]
    assert list(doc["paths"]["/depots/{depotId}/parcels"]) == ["get"]  # the POST of the same path is not kept
    assert doc["paths"]["/depots/{depotId}/parcels"]["get"]["operationId"] == "parcels.depots.parcels.list"
    assert doc["components"] == {"schemas": {}}


def test_discovery_parameters_take_their_types_and_repeated_becomes_an_array():
    params = _params(_from_discovery(), "/depots/{depotId}/parcels")
    assert params[("path", "depotId")] == {"name": "depotId", "in": "path", "required": True,
                                           "schema": {"type": "string", "default": "main"}}
    assert params[("query", "limit")]["schema"] == {"type": "integer", "format": "uint32", "default": 50,
                                                     "minimum": 1, "maximum": 500}
    assert params[("query", "tag")] == {"name": "tag", "in": "query", "required": False,
                                        "schema": {"type": "array", "items": {"type": "string"}}}
    assert params[("query", "express")]["schema"] == {"type": "boolean", "default": False}
    assert params[("query", "status")]["schema"] == {"type": "string", "enum": ["open", "shipped"]}


def test_discovery_api_wide_parameters_are_kept_only_when_listed():
    assert ("query", "alt") not in _params(_from_discovery(), "/depots/{depotId}/parcels")
    params = _params(_from_discovery(replace(LIST_PARCELS, query=("alt",))), "/depots/{depotId}/parcels")
    assert params[("query", "alt")]["schema"] == {"type": "string", "enum": ["json", "media"], "default": "json"}


def test_discovery_subset_renders_deterministically():
    assert es.render(_from_discovery()) == es.render(_from_discovery())


@pytest.mark.parametrize("op, message", [
    (es.Operation(method="post", path="/depots/{depotId}/parcels", fields=("weight",)),
     "a discovery request body is not converted"),
    (es.Operation(method="get", path="/depots/{depotId}/parcels/{+name}", fields=()), "reserved expansion"),
    (es.Operation(method="get", path="/depots/{depotId}/boxes", fields=()), "absent from the source"),
])
def test_a_discovery_method_the_converter_cannot_take_fails_loudly(op, message):
    with pytest.raises(ValueError, match=message):
        _from_discovery(op)


def test_a_discovery_value_that_does_not_read_as_its_type_fails():
    spec = _discovery()
    spec["resources"]["depots"]["resources"]["parcels"]["methods"]["list"]["parameters"]["express"]["default"] = "no"
    with pytest.raises(ValueError, match="parameter express.default: 'no' is not a boolean"):
        es.discovery_document(spec, (LIST_PARCELS,))


def test_the_discovery_source_url_is_pinned_by_the_ref_and_names_the_transforms():
    fetched = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return PARCELS_DISCOVERY.read_bytes()

    doc = es.openapi_subset(PARCELS_D, "0123abc", "2026-11-19", fetch=fetch)
    assert fetched == ["https://raw.githubusercontent.com/example/discovery/0123abc/discoveries/parcels.v1.json"]
    assert doc["x-source"] == fetched[0] and doc["info"]["version"] == "v1-20261101"
    assert doc["x-transforms"] == [*es._TRANSFORMS, *es._DISCOVERY_TRANSFORMS, es._PARAMETER_TRANSFORM]


@pytest.mark.parametrize("fmt, low, high", [
    ("int32", -2147483648, 2147483647), ("uint32", 0, 4294967295),
    ("int64", -9223372036854775808, 9223372036854775807), ("uint64", 0, 18446744073709551615),
])
def test_a_discovery_integer_format_becomes_its_range(fmt, low, high):
    spec = _discovery()
    params = spec["resources"]["depots"]["resources"]["parcels"]["methods"]["list"]["parameters"]
    params["page"] = {"location": "query", "type": "integer", "format": fmt}
    params["limit"]["format"] = fmt  # the method's own minimum and maximum win over the format's range
    op = replace(LIST_PARCELS, query=("limit", "page"))
    doc = es.build_subset(es.discovery_document(spec, (op,)), replace(PARCELS_D, operations=(op,)), source="s",
                          version="v", retrieved="2026-11-19")
    by_name = _params(doc, "/depots/{depotId}/parcels")
    assert by_name[("query", "page")]["schema"] == {"type": "integer", "format": fmt, "minimum": low, "maximum": high}
    assert (by_name[("query", "limit")]["schema"]["minimum"], by_name[("query", "limit")]["schema"]["maximum"]) == (1, 500)


def test_a_string_typed_64_bit_discovery_parameter_gets_no_range():
    spec = _discovery()
    params = spec["resources"]["depots"]["resources"]["parcels"]["methods"]["list"]["parameters"]
    params["since"] = {"location": "query", "type": "string", "format": "uint64"}
    op = replace(LIST_PARCELS, query=("since",))
    doc = es.build_subset(es.discovery_document(spec, (op,)), replace(PARCELS_D, operations=(op,)), source="s",
                          version="v", retrieved="2026-11-19")
    assert _params(doc, "/depots/{depotId}/parcels")[("query", "since")]["schema"] == {"type": "string",
                                                                                      "format": "uint64"}


def test_a_deprecated_discovery_parameter_keeps_its_mark():
    spec = _discovery()
    spec["resources"]["depots"]["resources"]["parcels"]["methods"]["list"]["parameters"]["tag"]["deprecated"] = True
    params = _params(_from_discovery_spec(spec), "/depots/{depotId}/parcels")
    assert params[("query", "tag")]["deprecated"] is True
    assert "deprecated" not in params[("query", "limit")]


def _from_discovery_spec(spec: dict) -> dict:
    return es.build_subset(es.discovery_document(spec, (LIST_PARCELS,)), PARCELS_D, source="s", version="v",
                           retrieved="2026-11-19")

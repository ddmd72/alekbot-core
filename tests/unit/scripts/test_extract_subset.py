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

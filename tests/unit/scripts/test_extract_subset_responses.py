"""scripts/contracts/extract_subset.py, the response side: an operation's responses in its subset.

Offline: the OpenAPI source is an invented fragment (tests/fixtures/extract_subset/sprockets_openapi.yaml), the SDK
source invented pydantic models below; nothing is fetched.
"""
from dataclasses import replace
from pathlib import Path
from typing import Literal, Optional, Union

import pydantic
import pytest
from typing_extensions import Annotated, TypedDict

from scripts.contracts import extract_subset as es
from scripts.contracts import spec_drift as sd

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "sprockets_openapi.yaml"
REF = "#/components/schemas/"
UNREAD = {es.UNREAD: "not read by our adapter: any value"}

JSON = es.Response(fields=("teeth", "parts", "usage"), variants={"parts[]": ("cog", "spacer")}, reads="adapter.py:1-9")
STREAM = es.Response(media="text/event-stream", fields=("type", "text", "sprocket"),
                     variants={"": ("start", "delta", "done")})
OP = es.Operation(method="post", path="/sprockets", fields=("name", "parts"), responses=(JSON, STREAM))
SPROCKETS = es.Provider(name="sprockets", repo="example/sprockets", spec_file="openapi.yaml", operations=(OP,))


def _build(op: es.Operation = OP) -> dict:
    return es.build_subset(sd.parse(FIXTURE.read_bytes()), replace(SPROCKETS, operations=(op,)),
                           source="https://example.com/sprockets@abc", version="9.9.9", retrieved="2026-11-19")


def _op(doc: dict) -> dict:
    return doc["paths"]["/sprockets"]["post"]


def _schemas(doc: dict) -> dict:
    return doc["components"]["schemas"]


def _allow(**kw) -> es.Allowed:
    return es.Allowed(reason=kw.pop("reason", "the provider sends it (provider docs)"), date="2026-11-19", **kw)


# ------------------------------------------------------------------------------------------------------- the document
def test_each_response_lands_under_its_status_and_media_type_with_where_the_adapter_reads_it():
    responses = _op(_build())["responses"]
    assert list(responses) == ["200"]
    assert responses["200"]["description"] == "the parts our adapter reads: adapter.py:1-9"
    assert responses["200"]["content"]["application/json"]["schema"] == {"$ref": REF + "Sprocket"}
    assert set(responses["200"]["content"]) == {"application/json", "text/event-stream"}


def test_the_response_transforms_are_named_and_the_title_says_both_sides():
    doc = _build()
    assert doc["x-transforms"] == [*es._TRANSFORMS, *es._RESPONSE_TRANSFORMS]
    assert doc["info"]["title"] == "sprockets request and response subset"
    json_only = _build(replace(OP, responses=(JSON,)))
    assert json_only["x-transforms"] == [*es._TRANSFORMS, es._RESPONSE_TRANSFORMS[0]]


def test_without_responses_the_document_is_the_request_subset():
    doc = _build(replace(OP, responses=()))
    assert "responses" not in _op(doc) and doc["x-transforms"] == list(es._TRANSFORMS)
    assert doc["info"]["title"] == "sprockets request subset"


# ------------------------------------------------------------------------------------------------------ the top level
def test_read_fields_are_materialised_and_unread_ones_kept_by_name_with_any_value():
    sprocket = _schemas(_build())["Sprocket"]
    assert sprocket["additionalProperties"] is False
    assert sprocket["properties"]["teeth"] == {"type": "integer"}
    assert sprocket["properties"]["id"] == UNREAD and sprocket["properties"]["created_at"] == UNREAD
    assert sprocket["properties"]["echo"] == UNREAD  # not copied: the echoed request is never read
    assert sprocket["required"] == ["id", "teeth"]   # the provider's own required list, unread fields included


def test_below_the_top_level_objects_are_materialised_and_closed_and_deprecated_marks_kept():
    usage = _schemas(_build())["Usage"]
    assert usage == {"additionalProperties": False, "type": "object",
                     "properties": {"input_tokens": {"type": "integer"},
                                    "legacy_tokens": {"deprecated": True, "type": "integer"}}}


def test_response_variants_are_pruned_like_request_variants():
    parts = _schemas(_build())["Sprocket"]["properties"]["parts"]["items"]
    assert parts == {"anyOf": [{"$ref": REF + "Cog"}, {"$ref": REF + "Spacer"}]}
    assert "Chain" not in _schemas(_build(replace(OP, fields=("name",), responses=(JSON,))))


def test_a_stream_root_is_the_union_of_the_kept_events():
    stream = _op(_build())["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert stream == {"anyOf": [{"$ref": REF + "StartEvent"}, {"$ref": REF + "DeltaEvent"},
                                {"$ref": REF + "DoneEvent"}]}
    kept = _build(replace(OP, responses=(replace(STREAM, variants={"": ("delta",)}, fields=("type", "text")),)))
    assert _op(kept)["responses"]["200"]["content"]["text/event-stream"]["schema"] == {"$ref": REF + "DeltaEvent"}


def test_a_sub_schema_built_differently_from_the_root_gets_a_numbered_component():
    schemas = _schemas(_build())
    nested = schemas["StartEvent"]["properties"]["sprocket"]
    assert nested == {"$ref": REF + "Sprocket_2"}
    assert schemas["Sprocket_2"]["properties"]["echo"] == {"$ref": REF + "CreateSprocket"}  # nested: materialised


def test_a_component_built_the_same_way_for_the_request_and_a_response_is_one_component():
    schemas = _schemas(_build())
    assert schemas["CreateSprocket"]["properties"]["parts"]["items"] == {"$ref": REF + "Part"}
    assert {"$ref": REF + "Cog"} in schemas["Part"]["anyOf"]          # the request keeps every variant
    assert {"$ref": REF + "Cog"} in schemas["Sprocket"]["properties"]["parts"]["items"]["anyOf"]
    assert not any(name.startswith("Cog_") for name in schemas)


def test_the_request_part_does_not_change_when_responses_are_added():
    with_responses, without = _build(), _build(replace(OP, responses=()))
    assert _op(with_responses)["requestBody"] == _op(without)["requestBody"]
    assert _schemas(with_responses)["CreateSprocket"] == _schemas(without)["CreateSprocket"]


def test_a_response_given_as_a_ref_to_components_responses_is_resolved():
    error = es.Response(status="429", fields=("message",))
    doc = _build(replace(OP, responses=(error,)))
    assert _op(doc)["responses"]["429"]["content"]["application/json"]["schema"] == {"$ref": REF + "Error"}


# --------------------------------------------------------------------------------------------- the table's guards
@pytest.mark.parametrize("resp, message", [
    (replace(JSON, fields=("teeth", "parts", "weight")), r"listed fields absent from the source: \['weight'\]"),
    (replace(JSON, variants={"parts[]": ("cog", "gear")}), r"variants absent at parts\[\]: \['gear'\]"),
    (replace(JSON, variants={"partz[]": ("cog",)}), "under no listed field"),
    (replace(JSON, status="404"), "no 404 application/json response in the source"),
    (replace(JSON, media="text/plain"), "no 200 text/plain response in the source"),
    (replace(STREAM, variants={"": ("start", "stop")}), r"variants absent at <body>: \['stop'\]"),
    (replace(STREAM, fields=("type", "colour")), r"listed fields absent from the source: \['colour'\]"),
])
def test_a_response_entry_the_source_does_not_match_fails_loudly(resp, message):
    with pytest.raises(ValueError, match=message):
        _build(replace(OP, responses=(resp,)))


def test_a_response_allowed_variant_is_injected_as_a_closed_tagged_variant():
    stream = replace(STREAM, allowed=(_allow(path="", value="ping"),))
    schema = _op(_build(replace(OP, responses=(stream,))))["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert schema["anyOf"][-1] == {
        "type": "object", "properties": {"type": {"const": "ping"}}, "required": ["type"],
        "additionalProperties": False, "x-alek-extension": "the provider sends it (provider docs) (2026-11-19)",
    }


def test_a_response_allowed_variant_the_source_now_has_is_stale():
    stream = replace(STREAM, allowed=(_allow(path="", value="done"),))
    with pytest.raises(ValueError, match="stale ALLOWED at : the source now has variant 'done'"):
        _build(replace(OP, responses=(stream,)))


def test_a_response_allowed_top_level_field_is_added_next_to_the_source_fields():
    resp = replace(JSON, allowed=(_allow(path="", field="store", schema={"type": "boolean"}),))
    store = _schemas(_build(replace(OP, responses=(resp,))))["Sprocket"]["properties"]["store"]
    assert store == {"type": "boolean", "x-alek-extension": "the provider sends it (provider docs) (2026-11-19)"}


def test_a_response_allowed_top_level_field_the_source_now_has_is_stale():
    resp = replace(JSON, allowed=(_allow(path="", field="id", schema={"type": "string"}),))
    with pytest.raises(ValueError, match="stale ALLOWED at <body>: the source now has field 'id'"):
        _build(replace(OP, responses=(resp,)))


@pytest.mark.parametrize("allowed, message", [
    (_allow(path="", field="weight"), "a field needs a schema"),
    (_allow(path="header:x-trace", value="a"), "a response header is not supported"),
    (_allow(path="parts[]", reason=" ", value="chain"), "without a reason"),
])
def test_a_response_allowed_entry_of_an_unsupported_kind_is_refused(allowed, message):
    with pytest.raises(ValueError, match=message):
        _build(replace(OP, responses=(replace(JSON, allowed=(allowed,)),)))


# ------------------------------------------------------------------------------------------------------ SDK source
class SprocketCog(pydantic.BaseModel):
    type: Literal["cog"]
    size: int


class SprocketChain(pydantic.BaseModel):
    type: Literal["chain"]
    links: int


class SprocketUsage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    input_tokens: int
    output_tokens: Optional[int] = None


class SprocketReply(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    id: str
    parts: list[Annotated[Union[SprocketCog, SprocketChain], pydantic.Field(discriminator="type")]]
    usage: SprocketUsage
    free: dict = {}


class SprocketBody(TypedDict, total=False):
    name: str


# A request TypedDict and a response model with the same schema name and another shape.
ClashTwinParam = TypedDict("Twin", {"total": int}, total=False)
ClashTwinModel = pydantic.create_model("Twin", input_tokens=(int, ...))


class ClashBody(TypedDict, total=False):
    name: str
    twin: ClashTwinParam


class ClashReply(pydantic.BaseModel):
    twin: ClashTwinModel


_HERE = "tests.unit.scripts.test_extract_subset_responses"
SDK_REPLY = es.Response(fields=("parts", "usage", "free"), variants={"parts[]": ("cog",)},
                        sdk_model=f"{_HERE}:SprocketReply")
SDK_OP = es.Operation(method="post", path="/v1/sprockets", sdk_body=f"{_HERE}:SprocketBody", fields=("name",),
                      responses=(SDK_REPLY,))
SDK = es.Provider(name="sprockets", sdk="anthropic", server="https://api.sprockets.example.com", operations=(SDK_OP,))


def _sdk_build(op: es.Operation = SDK_OP) -> dict:
    return es.build_subset(es.sdk_document(op, "1.2.3"), replace(SDK, operations=(op,)), source="sprockets-sdk@1.2.3",
                           version="1.2.3", retrieved="2026-11-19")


def test_sdk_response_models_are_closed_and_free_form_fields_stay_free():
    schemas = _schemas(_sdk_build())
    reply = schemas["SprocketReply"]
    assert reply["additionalProperties"] is False and reply["properties"]["id"] == UNREAD
    assert schemas["SprocketUsage"]["additionalProperties"] is False      # extra="allow" in the SDK, closed here
    assert reply["properties"]["free"]["type"] == "object" and "properties" not in reply["properties"]["free"]
    assert reply["properties"]["parts"]["items"] == {"$ref": REF + "SprocketCog"}


def test_sdk_response_lands_in_the_source_document_under_its_status_and_media():
    source = es.sdk_document(SDK_OP, "1.2.3")
    assert source["paths"]["/v1/sprockets"]["post"]["responses"]["200"]["content"]["application/json"] == {
        "schema": {"$ref": REF + "SprocketReply"}}


def test_sdk_response_without_a_model_is_refused():
    op = replace(SDK_OP, responses=(replace(SDK_REPLY, sdk_model=""),))
    with pytest.raises(ValueError, match="an SDK source needs sdk_model on its 200 application/json response"):
        es.sdk_document(op, "1.2.3")


def test_sdk_response_schema_that_clashes_with_a_request_schema_is_refused():
    op = replace(SDK_OP, sdk_body=f"{_HERE}:ClashBody", fields=("name", "twin"),
                 responses=(es.Response(fields=("twin",), sdk_model=f"{_HERE}:ClashReply"),))
    with pytest.raises(ValueError, match="SDK schema Twin differs between the request and a response"):
        es.sdk_document(op, "1.2.3")


def test_the_sdk_subset_names_the_response_transforms():
    names = [t.split(":")[0] for t in es.sdk_subset(SDK, "2026-11-19")["x-transforms"]]
    assert names[-3:] == ["sdk-response-schema", "sdk-response-closed", "response-unread"]

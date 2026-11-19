"""The committed provider contracts (contracts/providers/<provider>/openapi.yaml) are sound and current.

Sound: every $ref resolves, every schema is valid Draft 2020-12, the source, retrieval date and transforms are named,
the file is under the size cap, every ALLOWED entry of the extractor's table is in the file with its reason. Current:
the Anthropic subset equals a fresh extraction from the installed SDK, so an SDK pin bump goes red until the subset is
regenerated (`make contract-extract PROVIDER=anthropic`). The OpenAI subset is pinned to a spec commit; a newer spec
is the weekly spec-drift run's business, not this test's.
"""
from pathlib import Path

import jsonschema
import pytest
import yaml

from scripts.contracts import extract_subset as es

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = REPO_ROOT / "contracts" / "providers"
PROVIDERS = sorted(es.PROVIDERS)


def _path(provider: str) -> Path:
    return CONTRACTS / provider / "openapi.yaml"


def _doc(provider: str) -> dict:
    return yaml.safe_load(_path(provider).read_text(encoding="utf-8"))


def _refs(node, out: list) -> list:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            out.append(node["$ref"])
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)
    return out


def _values(node, key: str, out: list) -> list:
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for value in node.values():
            _values(value, key, out)
    elif isinstance(node, list):
        for value in node:
            _values(value, key, out)
    return out


def test_every_table_provider_has_a_committed_subset_and_nothing_else_is_there():
    assert sorted(p.name for p in CONTRACTS.iterdir() if p.is_dir()) == PROVIDERS
    for provider in PROVIDERS:
        assert _path(provider).is_file(), provider


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_subset_names_its_source_date_and_transforms(provider):
    doc = _doc(provider)
    assert doc["openapi"] == "3.1.0"
    assert doc["x-source"] and doc["x-retrieved"] and doc["x-transforms"]
    if es.PROVIDERS[provider].repo:
        assert es.PROVIDERS[provider].repo in doc["x-source"]   # pinned by a commit in the URL
    else:
        assert doc["x-source"].endswith("@" + doc["info"]["version"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_subset_is_under_the_size_cap(provider):
    assert _path(provider).stat().st_size < es.MAX_BYTES


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_ref_resolves_and_every_schema_is_valid(provider):
    doc = _doc(provider)
    schemas = doc["components"]["schemas"]
    for ref in _refs(doc, []):
        assert ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[-1] in schemas, ref
    for name, schema in schemas.items():
        jsonschema.Draft202012Validator.check_schema(schema)
    table = {(o.path, o.method): o for o in es.PROVIDERS[provider].operations}
    for path, path_item in doc["paths"].items():
        for method, op in path_item.items():
            # A body exactly when the table lists body fields or a raw body (neither: no body, e.g. a Gmail GET).
            row = table[(path, method)]
            assert ("requestBody" in op) == bool(row.fields or row.raw_body), (method, path)
            if row.fields:
                # A JSON body: application/json is its only media type, and its schema is valid.
                assert list(op["requestBody"]["content"]) == ["application/json"], (method, path)
                jsonschema.Draft202012Validator.check_schema(op["requestBody"]["content"]["application/json"]["schema"])
            elif row.raw_body:
                # A raw body (an upload's bytes, never validated): its media type only, as a binary string.
                assert list(op["requestBody"]["content"]) == [row.raw_body], (method, path)
                schema = op["requestBody"]["content"][row.raw_body]["schema"]
                assert schema == {"type": "string", "format": "binary"}, (method, path, schema)
            for param in op.get("parameters") or []:
                jsonschema.Draft202012Validator.check_schema(param["schema"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_table_operation_is_in_the_subset(provider):
    doc = _doc(provider)
    for op in es.PROVIDERS[provider].operations:
        assert op.method in doc["paths"][op.path], (op.method, op.path)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_allowed_entry_has_a_reason_and_is_in_the_subset(provider):
    extensions = _values(_doc(provider), "x-alek-extension", [])
    allowed = [a for op in es.PROVIDERS[provider].operations
               for a in (*op.allowed, *(a for r in op.responses for a in r.allowed))]
    for a in allowed:
        assert a.reason.strip() and a.date, a
        assert es._note(a) in extensions, a
    assert len(extensions) == len(allowed)


@pytest.mark.slow  # pydantic builds the SDK request schema (~0.6 s)
def test_the_anthropic_subset_is_what_the_installed_sdk_gives():
    committed = _path("anthropic").read_text(encoding="utf-8")
    retrieved = yaml.safe_load(committed)["x-retrieved"]
    fresh = es.render(es.sdk_subset(es.PROVIDERS["anthropic"], retrieved))
    assert fresh == committed, "the installed SDK differs from the subset: make contract-extract PROVIDER=anthropic"


# Free-form places in a committed subset: our own JSON Schemas (the PROVIDERS open paths) and the provider's own free-form
# JSON fields. Everything else is closed; a hand edit that opens an object (or adds an unlisted free-form one) fails.
FREE_FORM = {
    "openai": {
        "/components/schemas/FunctionTool/properties/parameters": "open path tools[].parameters: our tool's JSON Schema",
        "/components/schemas/TextResponseFormatJsonSchema/properties/schema":
            "open path text.format.schema: our output JSON Schema",
        "/components/schemas/FunctionTool/properties/output_schema/anyOf[0]":
            "FunctionTool.output_schema: the provider's own free-form JSON object",
    },
    "anthropic": {
        "/components/schemas/ToolParam/properties/input_schema": "open path tools[].input_schema: our tool's JSON Schema",
        "/components/schemas/JSONOutputFormatParam/properties/schema":
            "open path output_config.format.schema: our output JSON Schema",
        "/components/schemas/ToolUseBlockParam/properties/input": "a tool_use block's input: the model's own JSON arguments",
        "/components/schemas/ServerToolUseBlockParam/properties/input":
            "a server_tool_use block's input: the model's own JSON arguments",
        "/components/schemas/ToolParam/properties/input_examples/items": "ToolParam.input_examples: example JSON arguments",
        "/components/schemas/ToolUseBlock/properties/input":
            "a response tool_use block's input: the model's own JSON arguments",
        "/components/schemas/ServerToolUseBlock/properties/input":
            "a response server_tool_use block's input: the model's own JSON arguments",
    },
    "gmail": {},  # GETs: path and query parameters only, no body and no free-form place
    "graph": {},  # every JSON body closed; the upload's raw body is a binary string, not an object
}


def _open_places(node, path: str, unclosed: list, free: list) -> None:
    if isinstance(node, dict):
        extra = node.get("additionalProperties")
        if "properties" in node and extra is not False:
            unclosed.append(path)
        if extra is True or extra == {} or isinstance(extra, dict):
            free.append(path)
        elif node.get("type") == "object" and "properties" not in node and "anyOf" not in node and "allOf" not in node:
            free.append(path)
        for key, value in node.items():
            _open_places(value, f"{path}/{key}", unclosed, free)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _open_places(value, f"{path}[{i}]", unclosed, free)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_subset_is_closed_except_the_named_free_form_fields(provider):
    """A structural guard against hand edits of the committed YAML (the extractor writes it, never a person)."""
    unclosed: list = []
    free: list = []
    _open_places(_doc(provider), "", unclosed, free)
    assert unclosed == [], f"objects with properties that are not closed (additionalProperties: false): {unclosed}"
    assert sorted(free) == sorted(FREE_FORM[provider]), (
        "free-form places differ from the named list: unlisted "
        f"{sorted(set(free) - set(FREE_FORM[provider]))}, stale {sorted(set(FREE_FORM[provider]) - set(free))}")
    assert all(reason.strip() for reason in FREE_FORM[provider].values())

"""scripts/contracts/extract_subset.py: an ALLOWED gap of the value null, and a gap narrowed to one variant (P17).

Over the invented widgets spec (tests/fixtures/extract_subset/widgets_openapi.yaml): `parts[]` keeps the variants bolt
(a `length` number) and gear (an integer `teeth`). A `null` gap widens the node at its path with `const: null`; a
`variant` limits a null, value or new-field gap to the typed object of that `type`, since a path reaches every kept
variant of a union.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.contracts import extract_subset as es
from scripts.contracts import spec_drift as sd

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extract_subset" / "widgets_openapi.yaml"
OP = es.Operation(method="post", path="/widgets", fields=("name", "parts"), variants={"parts[]": ("bolt", "gear")})
NOTE = "our adapter sends it (widget_adapter.py) (2026-11-19)"


def _allow(**kw) -> es.Allowed:
    return es.Allowed(reason="our adapter sends it (widget_adapter.py)", date="2026-11-19", **kw)


def _schemas(*allowed: es.Allowed, spec: dict | None = None) -> dict:
    provider = es.Provider(name="widgets", repo="example/widgets-openapi", spec_file="openapi.yaml",
                           operations=(replace(OP, allowed=allowed),))
    doc = es.build_subset(spec or sd.parse(FIXTURE.read_bytes()), provider, source="https://example.com/widgets@abc",
                          version="9.9.9", retrieved="2026-11-19")
    return doc["components"]["schemas"]


def test_a_null_gap_widens_the_node_with_const_null():
    teeth = _schemas(_allow(path="parts[].teeth", null=True))["Gear"]["properties"]["teeth"]
    assert teeth == {"anyOf": [{"type": "integer"}, {"const": None, "x-alek-extension": NOTE}]}


def test_a_null_gap_the_source_now_accepts_is_stale():
    spec = sd.parse(FIXTURE.read_bytes())
    spec["components"]["schemas"]["Gear"]["properties"]["teeth"] = {"type": ["integer", "null"]}
    with pytest.raises(ValueError, match="stale ALLOWED at parts\\[\\].teeth: the source now accepts null"):
        _schemas(_allow(path="parts[].teeth", null=True), spec=spec)


def test_a_new_field_gap_without_a_variant_reaches_every_kept_variant():
    schemas = _schemas(_allow(path="parts[]", field="finish", schema={"type": "string"}))
    assert "finish" in schemas["Bolt"]["properties"] and "finish" in schemas["Gear"]["properties"]


def test_a_variant_narrows_a_new_field_gap_to_that_variant():
    schemas = _schemas(_allow(path="parts[]", field="finish", schema={"type": "string"}, variant="gear"))
    assert schemas["Gear"]["properties"]["finish"] == {"type": "string", "x-alek-extension": NOTE}
    assert "finish" not in schemas["Bolt"]["properties"]


def test_a_variant_narrows_a_null_gap_to_the_object_holding_the_node():
    spec = sd.parse(FIXTURE.read_bytes())
    spec["components"]["schemas"]["Gear"]["properties"]["length"] = {"type": "number"}
    schemas = _schemas(_allow(path="parts[].length", null=True, variant="gear"), spec=spec)
    assert schemas["Gear"]["properties"]["length"] == {
        "anyOf": [{"type": "number"}, {"const": None, "x-alek-extension": NOTE}]}
    assert schemas["Bolt"]["properties"]["length"] == {"type": "number"}


@pytest.mark.parametrize("allowed, message", [
    (_allow(path="parts[].teeth", null=True, variant="washer"), "narrowed to a variant never applied"),
    (_allow(path="parts[]", null=True), "ALLOWED null at the union 'parts\\[\\]'"),
    (_allow(path="parts[].teeth", null=True, value="none"), "exactly one of value / field / null"),
    (_allow(path="name", field="name", variant="gear"), "a variant narrows a value, null or new field only"),
    (_allow(path="header:x-widget-beta", null=True), "a header allows values only"),
])
def test_a_null_or_narrowed_gap_the_table_gets_wrong_is_refused(allowed, message):
    with pytest.raises(ValueError, match=message):
        _schemas(allowed)

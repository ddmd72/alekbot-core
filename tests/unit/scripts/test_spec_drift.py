"""scripts/contracts/spec_drift.py: extract the watched part of a published spec, diff it against the baseline, report.

Offline: every spec comes from tests/fixtures/spec_drift/ (invented fragments) through a fake fetch keyed by URL.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.contracts import spec_drift as sd

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "spec_drift"

OPENAPI = sd.Source("w_openapi", "openapi", "https://example.com/openapi.yaml",
                    ("post /widgets", "get /widgets/{widget_id}"), "src/adapters/widget_adapter.py")
DISCOVERY = sd.Source("w_discovery", "discovery", "https://example.com/discovery.json",
                      ("widgets.users.widgets.list", "widgets.users.widgets.parts.get"), "src/adapters/widget_adapter.py")
TELEGRAM = sd.Source("w_telegram", "telegram", "https://example.com/api.json",
                     ("method:sendWidget", "type:Widget", "type:Shape"), "src/adapters/widget_adapter.py")


def _raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _spec(name: str) -> dict:
    return sd.parse(_raw(name))


class FakeFetch:
    """URL -> bytes; a URL mapped to an exception raises it."""

    def __init__(self, routes: dict):
        self.routes = routes

    def __call__(self, url: str) -> bytes:
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


# ------------------------------------------------------------------------------------------------- normalisation
def test_normalize_drops_documentation_and_sorts_set_lists():
    node = {
        "description": "prose", "summary": "s", "title": "T", "example": 1, "x-oaiMeta": {"a": 1},
        "x-stainless-naming": "n", "required": ["b", "a"], "enum": ["z", "y"], "anyOf": [{"type": "b"}, {"type": "a"}],
        "properties": {"p": {"description": "gone", "type": "string"}}, "x-twilio": {"pii": {"handling": "standard"}},
    }
    assert sd.normalize(node) == {
        "required": ["a", "b"], "enum": ["y", "z"], "anyOf": [{"type": "b"}, {"type": "a"}],
        "properties": {"p": {"type": "string"}}, "x-twilio": {"pii": {"handling": "standard"}},
    }


def test_normalize_keeps_a_required_flag_and_a_deprecation():
    assert sd.normalize({"required": True, "deprecated": True, "default": 3}) == \
        {"required": True, "deprecated": True, "default": 3}


def test_parse_reads_json_and_yaml():
    assert sd.parse(b'{"a": [1]}') == {"a": [1]}
    assert sd.parse(b"a:\n  - 1\n") == {"a": [1]}


# ---------------------------------------------------------------------------------------------------- extraction
def test_openapi_extract_takes_watched_operations_and_their_ref_closure():
    out = sd.extract(OPENAPI, _spec("openapi_fragment.yaml"))
    assert set(out["items"]) == {"post /widgets", "get /widgets/{widget_id}"}   # /gadgets is not watched
    assert set(out["components"]) == {
        "#/components/schemas/CreateWidgetRequest", "#/components/schemas/Widget", "#/components/schemas/Part",
    }                                                                              # Part -> Widget cycle terminates
    assert out["meta"] == {"openapi": "3.1.0", "version": "1.4.0"}
    create = out["items"]["post /widgets"]
    assert "summary" not in create and "description" not in create and "x-oaiMeta" not in create
    request = out["components"]["#/components/schemas/CreateWidgetRequest"]
    assert request["required"] == ["colour", "size"]
    assert request["properties"]["colour"] == {"type": "string", "enum": ["amber", "plum", "teal"]}


def test_openapi_extract_keeps_path_level_parameters_with_the_operation():
    get = sd.extract(OPENAPI, _spec("openapi_fragment.yaml"))["items"]["get /widgets/{widget_id}"]
    assert [p["name"] for p in get["pathParameters"]] == ["widget_id"]
    assert [p["name"] for p in get["parameters"]] == ["include"]


def test_openapi_unresolvable_ref_is_recorded_as_none():
    spec = {"paths": {"/a": {"post": {"responses": {"200": {"$ref": "#/components/responses/Gone"}}}}}}
    out = sd.extract(sd.Source("x", "openapi", "u", ("post /a",), ""), spec)
    assert out["components"] == {"#/components/responses/Gone": None}


def test_openapi_missing_watched_operation_is_absent_from_the_extract():
    source = sd.Source("x", "openapi", "u", ("post /widgets", "delete /widgets"), "")
    assert set(sd.extract(source, _spec("openapi_fragment.yaml"))["items"]) == {"post /widgets"}


def test_discovery_extract_finds_nested_methods_by_id_and_follows_schema_refs():
    out = sd.extract(DISCOVERY, _spec("discovery_fragment.json"))
    assert set(out["items"]) == {"widgets.users.widgets.list", "widgets.users.widgets.parts.get"}
    assert set(out["components"]) == {"ListWidgetsResponse", "Widget", "Part"}   # Unrelated is not reached
    assert out["meta"] == {"revision": "20260301", "version": "v1"}
    listing = out["items"]["widgets.users.widgets.list"]
    assert listing["scopes"] == sorted(listing["scopes"])
    assert "description" not in listing["parameters"]["userId"]
    assert "enumDescriptions" not in out["components"]["Widget"]["properties"]["colour"]


def test_telegram_extract_reduces_fields_and_keeps_returns_and_subtypes():
    out = sd.extract(TELEGRAM, _spec("telegram_fragment.json"))
    assert out["meta"] == {"version": "Bot API 1.0", "release_date": "March 1, 2026"}
    assert out["components"] == {}
    send = out["items"]["method:sendWidget"]
    assert send["returns"] == ["Message"]
    assert send["fields"][0] == {"name": "chat_id", "types": ["Integer", "String"], "required": True}
    assert out["items"]["type:Widget"]["fields"][1] == {"name": "size", "types": ["Integer"], "required": False}
    assert out["items"]["type:Shape"] == {"fields": [], "subtypes": ["ShapeCircle", "ShapeSquare"]}
    assert "method:deleteWidget" not in out["items"]


# ---------------------------------------------------------------------------------------------------------- diff
def test_diff_reports_added_removed_and_changed_keys():
    old = {"a": 1, "b": {"c": "x"}, "gone": True}
    new = {"a": 2, "b": {"c": "x", "d": None}}
    assert sd.diff(old, new) == [
        sd.Change("a", "changed", 1, 2), sd.Change("b.d", "added", new=None), sd.Change("gone", "removed", old=True),
    ]


def test_diff_keys_lists_of_named_dicts_by_name():
    old = [{"name": "a", "required": True}, {"name": "b", "required": False}]
    new = [{"name": "b", "required": True}, {"name": "a", "required": True}, {"name": "c"}]
    assert sd.diff(old, new, "fields") == [
        sd.Change("fields.b.required", "changed", False, True), sd.Change("fields.c", "added", new={"name": "c"}),
    ]


def test_diff_treats_other_lists_as_multisets():
    assert sd.diff([{"$ref": "A"}, {"$ref": "B"}], [{"$ref": "B"}, {"$ref": "A"}]) == []
    assert sd.diff(["a", "b"], ["b", "c"], "enum") == [
        sd.Change("enum[]", "removed", old="a"), sd.Change("enum[]", "added", new="c"),
    ]


def test_diff_type_change_is_one_change():
    assert sd.diff({"t": "string"}, {"t": ["string", "null"]}) == [sd.Change("t", "changed", "string", ["string", "null"])]


# ------------------------------------------------------------------------------------------------------ check/run
def _baseline(tmp_path: Path, source: sd.Source, fixture: str) -> Path:
    return sd.update(source, FakeFetch({source.url: _raw(fixture)}), tmp_path, "2026-03-01")


def test_update_writes_a_deterministic_baseline(tmp_path):
    path = _baseline(tmp_path, OPENAPI, "openapi_fragment.yaml")
    first = path.read_text()
    doc = json.loads(first)
    assert doc["retrieved"] == "2026-03-01" and doc["watch"] == list(OPENAPI.watch) and doc["url"] == OPENAPI.url
    _baseline(tmp_path, OPENAPI, "openapi_fragment.yaml")
    assert path.read_text() == first


def test_update_refuses_a_watched_item_absent_upstream(tmp_path):
    source = sd.Source("w_openapi", "openapi", OPENAPI.url, ("post /widgets", "delete /widgets"), "")
    with pytest.raises(ValueError, match="delete /widgets"):
        sd.update(source, FakeFetch({source.url: _raw("openapi_fragment.yaml")}), tmp_path, "2026-03-01")
    assert not (tmp_path / "w_openapi.json").exists()


def test_check_same_when_upstream_unchanged_even_if_its_version_moved(tmp_path):
    _baseline(tmp_path, OPENAPI, "openapi_fragment.yaml")
    spec = _spec("openapi_fragment.yaml")
    spec["info"]["version"] = "9.9.9"
    spec["paths"]["/gadgets"]["post"]["operationId"] = "renamed"   # not watched
    spec["components"]["schemas"]["Widget"]["description"] = "reworded"
    result = sd.check(OPENAPI, FakeFetch({OPENAPI.url: json.dumps(spec).encode()}), tmp_path)
    assert result.status == "same" and result.changes == []
    assert result.meta["version"] == "9.9.9" and result.baseline_meta["version"] == "1.4.0"
    assert result.baseline_retrieved == "2026-03-01"


def test_check_reports_drift_in_an_operation_and_in_a_reached_component(tmp_path):
    _baseline(tmp_path, OPENAPI, "openapi_fragment.yaml")
    spec = _spec("openapi_fragment.yaml")
    request = spec["components"]["schemas"]["CreateWidgetRequest"]
    request["properties"]["size"]["default"] = 5
    request["required"].append("parts")
    del spec["components"]["schemas"]["Part"]["properties"]["name"]
    spec["paths"]["/widgets"]["post"]["deprecated"] = True
    result = sd.check(OPENAPI, FakeFetch({OPENAPI.url: yaml.safe_dump(spec).encode()}), tmp_path)
    assert result.status == "drift"
    assert set(c.line() for c in result.changes) == {
        '~ components.#/components/schemas/CreateWidgetRequest.properties.size.default: 3 -> 5',
        '+ components.#/components/schemas/CreateWidgetRequest.required[]: "parts"',
        '- components.#/components/schemas/Part.properties.name: {"type": "string"}',
        '+ items.post /widgets.deprecated: true',
    }


def test_check_reports_a_watched_operation_removed_upstream(tmp_path):
    _baseline(tmp_path, OPENAPI, "openapi_fragment.yaml")
    spec = _spec("openapi_fragment.yaml")
    del spec["paths"]["/widgets"]["post"]
    result = sd.check(OPENAPI, FakeFetch({OPENAPI.url: json.dumps(spec).encode()}), tmp_path)
    assert result.status == "drift"
    assert ("items.post /widgets", "removed") in {(c.path, c.kind) for c in result.changes}


def test_check_telegram_field_turns_required(tmp_path):
    _baseline(tmp_path, TELEGRAM, "telegram_fragment.json")
    spec = _spec("telegram_fragment.json")
    spec["types"]["Widget"]["fields"][1]["required"] = True
    result = sd.check(TELEGRAM, FakeFetch({TELEGRAM.url: json.dumps(spec).encode()}), tmp_path)
    assert [c.line() for c in result.changes] == ["~ items.type:Widget.fields.size.required: false -> true"]


def test_check_fetch_error_and_missing_baseline_are_statuses(tmp_path):
    failed = sd.check(OPENAPI, FakeFetch({OPENAPI.url: RuntimeError("HTTP 503")}), tmp_path)
    assert failed.status == "error" and "HTTP 503" in failed.error
    fresh = sd.check(DISCOVERY, FakeFetch({DISCOVERY.url: _raw("discovery_fragment.json")}), tmp_path)
    assert fresh.status == "no-baseline"


# ------------------------------------------------------------------------------------------------------- reports
def _drifted(tmp_path: Path, n_extra_fields: int = 0) -> list[sd.Result]:
    _baseline(tmp_path, TELEGRAM, "telegram_fragment.json")
    spec = _spec("telegram_fragment.json")
    spec["types"]["Widget"]["fields"] += [
        {"name": f"field_{i:04d}", "types": ["String"], "required": False} for i in range(n_extra_fields)
    ]
    del spec["methods"]["sendWidget"]["fields"][2]
    drift = sd.check(TELEGRAM, FakeFetch({TELEGRAM.url: json.dumps(spec).encode()}), tmp_path)
    error = sd.Result("w_openapi", "error", error="RuntimeError: HTTP 503")
    return [drift, error]


def test_render_md_lists_every_source_and_the_drift_lines(tmp_path):
    md = sd.render_md(_drifted(tmp_path, 1), [TELEGRAM, OPENAPI])
    assert "| w_telegram | drift | 1 | 0 | 1 | 2026-03-01 |" in md
    assert "| w_openapi | error | 0 | 0 | 0 | - | - |" in md
    assert "Used by: src/adapters/widget_adapter.py" in md
    assert md.index("- items.method:sendWidget.fields.parse_mode") < md.index("+ items.type:Widget.fields.field_0000")
    assert "RuntimeError: HTTP 503" in md


def test_annotations_one_notice_then_a_warning_per_flagged_source(tmp_path):
    report = {"results": [r.to_json() for r in _drifted(tmp_path, 1)]}
    report["results"].append(sd.Result("w_discovery", "same").to_json())
    lines = sd.annotations(report)
    assert lines[0] == "::notice title=Spec drift verdict::w_telegram drift (-1 ~0 +1); w_openapi error; w_discovery same"
    assert [line.split("::")[1] for line in lines[1:]] == ["warning title=Spec drift w_telegram", "warning title=Spec drift w_openapi"]
    assert "%0A- items.method:sendWidget.fields.parse_mode" in lines[1]
    assert all("\n" not in line for line in lines)


def test_annotation_is_capped_and_says_how_many_lines_were_cut(tmp_path):
    report = {"results": [r.to_json() for r in _drifted(tmp_path, 200)]}
    warning = sd.annotations(report)[1]
    message = warning.split("::", 2)[2]
    assert len(message.replace("%0A", "\n").replace("%25", "%")) <= sd.ANNOTATION_LIMIT
    cut = re.search(r"… (\d+) more", message)
    assert cut and 0 < int(cut.group(1)) < 201


def test_annotations_cap_the_warnings_per_step():
    rows = [sd.Result(f"s{i}", "error", error="boom").to_json() for i in range(15)]
    lines = sd.annotations({"results": rows})
    assert len(lines) == 1 + sd.MAX_WARNINGS


def test_annotation_escapes_percent_and_newlines():
    row = sd.Result("s", "error", error="50% done\r\nnext").to_json()
    assert sd.annotations({"results": [row]})[1].endswith("::error%0A50%25 done%0D%0Anext")


# ----------------------------------------------------------------------------------------------------------- CLI
def test_main_writes_reports_and_exits_zero_on_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "SOURCES", (TELEGRAM,))
    _baseline(tmp_path, TELEGRAM, "telegram_fragment.json")
    spec = _spec("telegram_fragment.json")
    spec["methods"]["sendWidget"]["returns"] = ["Boolean"]
    fetch = FakeFetch({TELEGRAM.url: json.dumps(spec).encode()})
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"
    assert sd.main(["--out-md", str(out_md), "--out-json", str(out_json)], fetch=fetch, baseline_dir=tmp_path) == 0
    report = json.loads(out_json.read_text())
    assert report["results"][0]["status"] == "drift" and "| w_telegram | drift |" in out_md.read_text()


def test_main_exits_one_when_no_source_could_be_fetched(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sd, "SOURCES", (TELEGRAM, OPENAPI))
    fetch = FakeFetch({TELEGRAM.url: OSError("down"), OPENAPI.url: OSError("down")})
    assert sd.main(["--out-json", str(tmp_path / "r.json")], fetch=fetch, baseline_dir=tmp_path) == 1
    assert "::error::" in capsys.readouterr().out


def test_main_update_exits_two_when_a_source_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "SOURCES", (TELEGRAM, OPENAPI))
    fetch = FakeFetch({TELEGRAM.url: _raw("telegram_fragment.json"), OPENAPI.url: OSError("down")})
    assert sd.main(["--update"], fetch=fetch, baseline_dir=tmp_path) == 2
    assert (tmp_path / "w_telegram.json").exists() and not (tmp_path / "w_openapi.json").exists()


def test_main_annotations_reads_a_report(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"results": [sd.Result("s", "same").to_json()]}))
    assert sd.main(["--annotations", str(path)]) == 0
    assert capsys.readouterr().out == "::notice title=Spec drift verdict::s same\n"


def test_main_refuses_an_unknown_source():
    with pytest.raises(SystemExit, match="unknown source"):
        sd.main(["--source", "nope"])


# ------------------------------------------------------------------------------------- the committed baselines
@pytest.mark.parametrize("source", sd.SOURCES, ids=lambda s: s.name)
def test_every_source_has_a_baseline_matching_its_manifest(source):
    path = sd.BASELINE_DIR / f"{source.name}.json"
    assert path.exists(), f"run: python -m scripts.contracts.spec_drift --update --source {source.name}"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert (doc["kind"], doc["url"], doc["watch"]) == (source.kind, source.url, list(source.watch)), \
        f"the manifest changed: python -m scripts.contracts.spec_drift --update --source {source.name}"
    assert set(doc["items"]) == set(source.watch)


def test_no_baseline_without_a_source():
    names = {s.name for s in sd.SOURCES}
    assert {p.stem for p in sd.BASELINE_DIR.glob("*.json")} == names

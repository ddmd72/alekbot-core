import importlib.util
import os

# Load the non-package tooling module by path (repo's established pattern;
# see tests/unit/scripts/test_migrate_to_embedding_v2.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))  # tests/unit/firestore_utils -> repo root
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_serializer.py")
_spec = importlib.util.spec_from_file_location("snapshot_serializer", _MODPATH)
ser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ser)


def test_token_round_trip_preserves_content_and_metadata():
    doc = {
        "token_id": "COGNITIVE_PROCESS_SMART",
        "category": "cognitive_process",
        "class": "COGNITIVE_PROCESS",
        "content": "cognitive_process {\n    INTENT → delegate → FORMAT\n}\n",
        "metadata": {"author": "system", "version": 3},
    }
    text = ser.token_to_file(doc)
    assert text.startswith("---\n")
    assert "cognitive_process {" in text  # body is readable, real newlines
    assert ser.token_from_file(text) == doc


def test_token_body_with_internal_delimiter_round_trips():
    doc = {
        "token_id": "X",
        "category": "c",
        "class": "C",
        "content": "line1\n---\nline2",  # body itself contains a --- line
        "metadata": {},
    }
    assert ser.token_from_file(ser.token_to_file(doc)) == doc


def test_token_to_file_drops_volatile_keys():
    doc = {"token_id": "X", "category": "c", "class": "C", "content": "x",
           "metadata": {}, "created_at": "2026-01-01", "updated_at": "2026-02-02"}
    parsed = ser.token_from_file(ser.token_to_file(doc))
    assert "created_at" not in parsed and "updated_at" not in parsed
    assert parsed["content"] == "x"


def test_blueprint_round_trip():
    doc = {"blueprint_id": "universal_agent_v1", "outer_class": "agent",
           "class_order": ["A", "B", "C"]}
    assert ser.doc_from_yaml(ser.doc_to_yaml(doc)) == doc


def test_profile_round_trip():
    doc = {"blueprint_id": "universal_agent_v1",
           "tokens": {"COGNITIVE_PROCESS_SMART": {"order": 10, "non_overridable": True}}}
    assert ser.doc_from_yaml(ser.doc_to_yaml(doc)) == doc


def test_doc_to_yaml_drops_volatile_keys():
    doc = {"blueprint_id": "B", "outer_class": "a", "class_order": [], "updated_at": "x"}
    assert "updated_at" not in ser.doc_from_yaml(ser.doc_to_yaml(doc))


def test_pii_guard_flags_user_keyed_fields():
    assert ser.is_pii_doc("COGNITIVE_PROCESS_SMART", {"user_id": "u1", "content": "x"}) is True
    assert ser.is_pii_doc("X", {"account_id": "a1"}) is True


def test_pii_guard_flags_uuid_like_doc_id():
    assert ser.is_pii_doc("3f2504e0-4f89-41d3-9a0c-0305e82c3301", {"content": "x"}) is True
    assert ser.is_pii_doc("0123456789abcdef0123456789abcdef", {"content": "x"}) is True


def test_pii_guard_allows_named_system_tokens():
    assert ser.is_pii_doc("COGNITIVE_PROCESS_SMART",
                          {"token_id": "COGNITIVE_PROCESS_SMART", "content": "x"}) is False


def test_relpath_layout():
    assert ser.relpath_for("tokens_system", "X") == "tokens/system/X.groovy"
    assert ser.relpath_for("tokens_user", "X") == "tokens/user/X.groovy"
    assert ser.relpath_for("blueprints", "B") == "blueprints/B.yaml"
    assert ser.relpath_for("profiles", "P") == "profiles/P.yaml"


def test_plan_snapshot_serializes_and_skips_pii():
    fetched = {
        "tokens_system": {
            "COGNITIVE_PROCESS_SMART": {"token_id": "COGNITIVE_PROCESS_SMART",
                                        "category": "c", "class": "C", "content": "x", "metadata": {}},
            "deadbeefdeadbeefdeadbeefdeadbeef": {"content": "leaked", "user_id": "u1"},  # PII
        },
        "tokens_user": {},
        "blueprints": {"universal_agent_v1": {"blueprint_id": "universal_agent_v1",
                                              "outer_class": "a", "class_order": []}},
        "profiles": {},
    }
    files, skipped = ser.plan_snapshot(fetched)
    assert "tokens/system/COGNITIVE_PROCESS_SMART.groovy" in files
    assert "blueprints/universal_agent_v1.yaml" in files
    assert "tokens_system/deadbeefdeadbeefdeadbeefdeadbeef" in skipped
    assert not any("deadbeef" in p for p in files)  # PII doc not written


def test_token_to_file_coerces_none_content():
    doc = {"token_id": "X", "category": "c", "class": "C", "content": None, "metadata": {}}
    rt = ser.token_from_file(ser.token_to_file(doc))
    assert rt["content"] == ""  # None treated as empty body

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))  # tests/unit/firestore_utils -> repo root
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_upload.py")
_spec = importlib.util.spec_from_file_location("snapshot_upload", _MODPATH)
up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(up)


def _write(base, rel, text):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_parse_file_token(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    text = up.serializer.token_to_file(
        {"token_id": "X", "category": "c", "class": "C", "content": "body\ntext", "metadata": {}}
    )
    path = _write(str(tmp_path), "tokens/system/X.groovy", text)
    kind, doc_id, doc = up.parse_file(path)
    assert kind == "tokens_system"
    assert doc_id == "X"
    assert doc["content"] == "body\ntext"


def test_parse_file_blueprint_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    bp = _write(str(tmp_path), "blueprints/B.yaml", up.serializer.doc_to_yaml({"blueprint_id": "B", "outer_class": "a", "class_order": []}))
    pr = _write(str(tmp_path), "profiles/P.yaml", up.serializer.doc_to_yaml({"blueprint_id": "B", "tokens": {}}))
    assert up.parse_file(bp)[0] == "blueprints"
    assert up.parse_file(pr)[0] == "profiles"


def test_parse_file_rejects_unknown_path(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "_SNAPSHOT_DIR", str(tmp_path))
    bad = _write(str(tmp_path), "random/thing.txt", "x")
    with pytest.raises(ValueError):
        up.parse_file(bad)


def test_diff_doc_reports_changed_new_kept():
    current = {"content": "old", "created_at": "2026-01-01"}
    new = {"content": "new", "category": "c"}
    lines = up.diff_doc(current, new)
    assert any(l.startswith("  M content") for l in lines)
    assert any(l.startswith("  + category") for l in lines)
    assert any("created_at" in l and "merge keeps" in l for l in lines)


def test_confirm_aborts_non_interactive(monkeypatch):
    def _raise(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise)
    assert up._confirm("X") is False


def test_confirm_requires_exact_id(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "X")
    assert up._confirm("X") is True
    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert up._confirm("X") is False

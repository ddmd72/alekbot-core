import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))  # tests/unit/firestore_utils -> repo root
_MODPATH = os.path.join(_ROOT, "firestore_utils", "snapshot_pull.py")
_spec = importlib.util.spec_from_file_location("snapshot_pull", _MODPATH)
pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pull)


def test_write_then_check_is_clean(tmp_path):
    base = str(tmp_path)
    files = {"tokens/system/A.groovy": "---\nx: 1\n---\nbody", "blueprints/B.yaml": "k: v\n"}
    pull.write_snapshot(files, base_dir=base)
    assert os.path.exists(os.path.join(base, "tokens/system/A.groovy"))
    assert not os.path.exists(os.path.join(base, "README.md"))  # README is hand-maintained, not written by the pull
    assert pull.check_snapshot(files, base_dir=base) == []  # no drift right after write


def test_write_preserves_hand_maintained_readme(tmp_path):
    base = str(tmp_path)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "README.md"), "w", encoding="utf-8") as f:
        f.write("hand-written")
    pull.write_snapshot({"tokens/system/A.groovy": "x"}, base_dir=base)  # B not in source
    # README is not a mirrored token: never created by write, never deleted as an orphan
    with open(os.path.join(base, "README.md"), encoding="utf-8") as f:
        assert f.read() == "hand-written"


def test_check_detects_added_removed_modified(tmp_path):
    base = str(tmp_path)
    pull.write_snapshot({"tokens/system/A.groovy": "v1", "profiles/P.yaml": "p\n"}, base_dir=base)
    drift = pull.check_snapshot(
        {"tokens/system/A.groovy": "v2", "blueprints/NEW.yaml": "n\n"}, base_dir=base
    )
    assert any(d.startswith("M tokens/system/A.groovy") for d in drift)
    assert any(d.startswith("+ blueprints/NEW.yaml") for d in drift)
    assert any(d.startswith("- profiles/P.yaml") for d in drift)


def test_write_removes_orphans(tmp_path):
    base = str(tmp_path)
    pull.write_snapshot({"tokens/system/A.groovy": "x", "tokens/system/B.groovy": "y"}, base_dir=base)
    pull.write_snapshot({"tokens/system/A.groovy": "x"}, base_dir=base)  # B gone from source
    assert not os.path.exists(os.path.join(base, "tokens/system/B.groovy"))

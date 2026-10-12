"""scripts/infrastructure/deploy_firestore_indexes.py applies single-field overrides (USER_DRIVE_INDEXING_RFC §12.5):
an entry in Firebase's fieldOverrides shape with `"indexes": []` becomes `gcloud firestore indexes fields update
<field> --disable-indexes`; any other override is refused as a failure, never applied half; a gcloud failure is
returned like a failed composite. gcloud is never called."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "deploy_firestore_indexes.py"

_ENTRIES = [
    {"collectionGroup": "chunks", "queryScope": "COLLECTION",
     "fields": [{"fieldPath": "user_id", "order": "ASCENDING"}, {"fieldPath": "item_id", "order": "ASCENDING"}]},
    {"collectionGroup": "chunks", "fieldPath": "text", "indexes": []},
]


def _deploy(tmp_path, entries, result):
    path = tmp_path / "indexes.json"
    path.write_text(json.dumps(entries))
    spec = importlib.util.spec_from_file_location("deploy_firestore_indexes", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands = []
    module.run_command = lambda cmd: commands.append(cmd) or result
    return module.deploy_indexes(str(path), "p", "d"), commands


def test_an_exemption_disables_the_fields_indexes(tmp_path):
    failures, commands = _deploy(tmp_path, _ENTRIES, SimpleNamespace(returncode=0, stderr=""))
    assert failures == []
    assert commands[1] == ["gcloud", "firestore", "indexes", "fields", "update", "text", "--collection-group=chunks",
                           "--project=p", "--database=d", "--disable-indexes", "--async"]
    assert commands[0][3] == "composite"


def test_any_other_override_is_refused_without_a_call(tmp_path):
    entry = {"collectionGroup": "chunks", "fieldPath": "text", "indexes": [{"order": "ASCENDING"}]}
    failures, commands = _deploy(tmp_path, [entry], SimpleNamespace(returncode=0, stderr=""))
    assert failures == ["chunks.text"] and commands == []


def test_a_failed_exemption_is_returned(tmp_path):
    failures, _ = _deploy(tmp_path, _ENTRIES, SimpleNamespace(returncode=1, stderr="PERMISSION_DENIED"))
    assert failures == ["chunks", "chunks.text"]

"""The email card vector index script (email index v2, slice 8): `make deploy-indexes` skips vector indexes, so the owner
creates this one with the script; its field configs must equal the declared entry in config/firestore.indexes.json
(card_state, user_id, card_vector 768 flat), on the facts collection group, so the index the script creates is the one
the card search's kNN query needs."""
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = (_ROOT / "scripts" / "infrastructure" / "create_email_card_vector_index.sh").read_text()
_GROUP = "development_domain_email_facts_v1"


def _declared():
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    (entry,) = [i for i in raw if i["collectionGroup"] == _GROUP and "fields" in i
                and i["fields"][-1]["fieldPath"] == "card_vector"]
    return entry


def _script_fields():
    out = []
    for value in re.findall(r"--field-config=(\S+)", _SCRIPT):
        parts = dict(p.split("=", 1) for p in re.split(r",(?=field-path=|order=)", value.rstrip(" \\")))
        out.append((parts["field-path"], parts.get("order", "VECTOR")))
    return out


def test_the_script_creates_the_declared_vector_index():
    declared = _declared()
    assert _script_fields() == [(f["fieldPath"], f.get("order", "VECTOR")) for f in declared["fields"]]
    assert declared["fields"][-1]["vectorConfig"] == {"dimension": 768, "flat": {}}
    assert """VECTOR_CONFIG='{"dimension":"768", "flat": "{}"}'""" in _SCRIPT
    assert "--query-scope=COLLECTION" in _SCRIPT and declared["queryScope"] == "COLLECTION"


def test_the_script_targets_the_facts_collection_in_the_named_database():
    assert 'COLLECTION="${COLLECTION_PREFIX-development_}domain_email_facts_v1"' in _SCRIPT
    assert 'DATABASE_ID="us-production"' in _SCRIPT
    assert 'PROJECT_ID="${PROJECT_ID:?' in _SCRIPT          # never a hard-coded project
    assert _SCRIPT.count("create_index \"") == 1

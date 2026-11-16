"""Provisioning of Stage 3's card read (email index v2 §6.7, slice 9): the settled read (`card_state in`, ordered by
`indexed_at`, unconsolidated only) has its composite declared in config/firestore.indexes.json; the stale read keeps the v1
composite. Both must be READY before the code deploys."""
import json
from pathlib import Path

GROUP = "development_domain_email_facts_v1"
_RAW = json.loads((Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json").read_text())


def _composites():
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "VECTOR") for f in idx["fields"]]
            for idx in _RAW
            if idx["collectionGroup"] == GROUP and "fields" in idx and idx.get("queryScope") == "COLLECTION"]


def test_the_settled_read_has_its_composite():
    assert [("user_id", "ASCENDING"), ("consolidated_at", "ASCENDING"), ("card_state", "ASCENDING"),
            ("indexed_at", "ASCENDING")] in _composites()


def test_the_stale_read_keeps_the_v1_composite():
    assert [("user_id", "ASCENDING"), ("consolidated_at", "ASCENDING"), ("indexed_at", "ASCENDING")] in _composites()

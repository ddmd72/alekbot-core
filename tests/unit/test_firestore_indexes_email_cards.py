"""Provisioning of the v2 email card reads (email index v2 §8, slice 6): the six composite indexes and the four
single-field exemptions on the live v1 facts collection group are declared in config/firestore.indexes.json, and every
key-list field the card index port can query has its composite."""
import json
from pathlib import Path

from src.domain.email_card_lifecycle import CARD_FIELDS

GROUP = "development_domain_email_facts_v1"
_RAW = json.loads((Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json").read_text())


def _composites():
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "VECTOR") for f in idx["fields"]]
            for idx in _RAW if idx["collectionGroup"] == GROUP and "fields" in idx]


def test_each_key_list_has_its_contains_composite_under_user_and_state():
    for field in ("about_keys", "name_keys", "people_keys", "month_keys"):
        assert [("user_id", "ASCENDING"), ("card_state", "ASCENDING"), (field, "CONTAINS")] in _composites()


def test_the_vector_composite_is_v1s_shape_on_card_state():
    index = next(i for i in _RAW if i["collectionGroup"] == GROUP and "fields" in i
                 and i["fields"][-1]["fieldPath"] == "card_vector")
    assert [f["fieldPath"] for f in index["fields"]] == ["card_state", "user_id", "card_vector"]
    assert index["fields"][-1]["vectorConfig"] == {"dimension": 768, "flat": {}}


def test_the_counts_and_the_scan_composite():
    assert [("user_id", "ASCENDING"), ("card_state", "ASCENDING")] in _composites()


def test_the_four_exemptions():
    exempt = {i["fieldPath"] for i in _RAW if i["collectionGroup"] == GROUP and "fields" not in i
              and i.get("indexes") == []}
    assert exempt == {"card", "card_vector", "about_keys", "name_keys"}
    assert exempt <= set(CARD_FIELDS)


def test_the_v1_indexes_of_the_collection_are_still_declared():
    composites = _composites()
    assert [("user_id", "ASCENDING"), ("consolidated_at", "ASCENDING"), ("indexed_at", "ASCENDING")] in composites
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING")] in composites

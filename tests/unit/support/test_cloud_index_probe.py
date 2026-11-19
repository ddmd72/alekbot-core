"""The cloud gate's index readiness, offline (tests/support/cloud_indexes.py): the key that decides whether a wanted index
exists carries the direction of `__name__`, and the probe query `ensure_indexes` runs after READY needs exactly its
composite. H1's index model (tests/support/firestore_eval.py, checked against the test database by the parity scenarios)
is the judge: the probe is refused without the index and served with it, for every composite of the repository file."""
import json
from pathlib import Path

import pytest
from google.api_core.exceptions import FailedPrecondition

from tests.support.cloud_indexes import admin_index, existing_key, probe_query, wanted_key
from tests.support.firestore_eval import EvalDb

INDEXES = json.loads((Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json").read_text())
COMPOSITES = [i for i in INDEXES if "fields" in i and i.get("queryScope", "COLLECTION") == "COLLECTION"]
PROBED = [i for i in COMPOSITES if probe_query(EvalDb().collection(i["collectionGroup"]), i) is not None]

ARCHIVED = {"collectionGroup": "g", "queryScope": "COLLECTION",
            "fields": [{"fieldPath": "user_id", "order": "ASCENDING"}, {"fieldPath": "archived_at", "order": "DESCENDING"}]}


def _with_name(index, order):
    return dict(index, fields=index["fields"] + [{"fieldPath": "__name__", "order": order}])


def _label(index):
    return index["collectionGroup"] + ":" + ",".join(f["fieldPath"] for f in index["fields"])


def test_most_composites_of_the_file_have_a_probe():
    assert len(PROBED) > len(COMPOSITES) // 2


@pytest.mark.parametrize("index", PROBED, ids=_label)
async def test_the_probe_needs_exactly_its_composite(index):
    group = index["collectionGroup"]
    without = EvalDb(indexes=[])
    with pytest.raises(FailedPrecondition, match="requires an index"):
        await probe_query(without.collection(group), index).get()
    served = EvalDb(indexes=[index])
    assert await probe_query(served.collection(group), index).get() == []


def test_no_probe_for_an_index_of_equality_and_array_fields_only():
    merged = {"collectionGroup": "g", "fields": [{"fieldPath": "user_id", "order": "ASCENDING"},
                                                 {"fieldPath": "match_keys", "arrayConfig": "CONTAINS"}]}
    assert probe_query(EvalDb().collection("g"), merged) is None


def test_the_key_carries_the_name_direction_declared_or_implied():
    assert wanted_key(ARCHIVED) == wanted_key(_with_name(ARCHIVED, "DESCENDING"))      # implied: the last ordered field's
    assert wanted_key(ARCHIVED) != wanted_key(_with_name(ARCHIVED, "ASCENDING"))
    assert wanted_key(ARCHIVED)[2] == "DESCENDING"


@pytest.mark.parametrize("index", COMPOSITES, ids=_label)
def test_an_index_created_from_the_file_has_the_key_it_was_wanted_under(index):
    assert existing_key(admin_index(index)) == wanted_key(index)

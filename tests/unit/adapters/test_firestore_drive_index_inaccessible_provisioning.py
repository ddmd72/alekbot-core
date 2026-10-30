"""Provisioning of the inaccessible-records read (USER_DRIVE_INDEXING_RFC §26.2, §26.9 step 3): `user_id ==`,
`inaccessible_since >=` the smallest timestamp (nulls excluded), ordered by it, so one composite index serves it."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, make_eval_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, TS_MIN, Db, executed, make_repo
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[3]


def _declared(collection):
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    return [[(f["fieldPath"], f.get("order")) for f in idx["fields"]]
            for idx in raw if idx["collectionGroup"] == collection and "fields" in idx]


@pytest.fixture
def repo(mock_env_config):
    db = Db()
    return make_repo(db, mock_env_config), db


async def test_the_query_and_its_index(repo):
    store, db = repo
    await store.inaccessible_records(U_SCOPE, 7)
    ops = [op for op in executed(db, RECS)[-1] if op[0] != "select"]
    assert ops == [("where", "connection_id", "==", "u"), ("where", "inaccessible_since", ">=", TS_MIN),
                   ("order_by", "inaccessible_since", "ASCENDING"), ("limit", 7)]
    assert [("connection_id", "ASCENDING"), ("inaccessible_since", "ASCENDING")] in _declared(
        "development_drive_index_records")


def test_the_smoke_script_runs_it(mock_env_config):
    spec = importlib.util.spec_from_file_location("drive_index_smoke",
                                                  _ROOT / "scripts" / "infrastructure" / "drive_index_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    names = [name for name, _ in smoke.checks(make_eval_repo(EvalDb(), mock_env_config), "smoke-user")]
    assert "inaccessible_records" in names

"""The credentials backfill script (MULTI_DRIVE_RFC §4.1, slice 1a): sets `integration` and `account_id` on records
saved before the 1a deploy; dry run by default; never touches `secret`; and the store's queries need no composite index."""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.support.firestore_eval import EvalDb

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "credentials_integration_backfill.py"
_INDEXES = _ROOT / "config" / "firestore.indexes.json"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("credentials_integration_backfill", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _seed(db, creds, users):
    for doc_id, data in creds.items():
        await db.collection("credentials").document(doc_id).set(data)
    for user_id, data in users.items():
        await db.collection("users").document(user_id).set(data)


def _old(user, cred, kind="gmail", **extra):
    return {"user_id": user, "credential_id": cred, "kind": kind, "secret": f"sealed-{cred}",
            "account": {"realm": "google", "subject": "sub-1"}, **extra}


async def _run(script, db, apply):
    return await script.backfill(db.collection("credentials"), db.collection("users"), apply=apply)


def _stored(db, doc_id):
    return db.collection("credentials").document(doc_id)


async def test_dry_run_counts_and_writes_nothing(script):
    db = EvalDb()
    await _seed(db, {"user-alex:cred-1": _old("user-alex", "cred-1")}, {"user-alex": {"account_id": "acct-1"}})
    report = await _run(script, db, apply=False)
    assert (report["scanned"], report["integration_set"], report["account_id_set"]) == (1, 1, 1)
    data = (await _stored(db, "user-alex:cred-1").get()).to_dict()
    assert "integration" not in data and "account_id" not in data
    assert "would set" in script.format_report(report, apply=False)


async def test_apply_sets_both_fields_and_never_touches_the_secret(script):
    db = EvalDb()
    await _seed(db, {"user-alex:cred-1": _old("user-alex", "cred-1"),
                     "user-clara:cred-2": _old("user-clara", "cred-2", kind="microsoft_onedrive")},
                {"user-alex": {"account_id": "acct-1"}, "user-clara": {"account_id": "acct-2"}})
    await _run(script, db, apply=True)
    one = (await _stored(db, "user-alex:cred-1").get()).to_dict()
    two = (await _stored(db, "user-clara:cred-2").get()).to_dict()
    assert (one["integration"], one["account_id"]) == ("mail", "acct-1")
    assert (two["integration"], two["account_id"]) == ("drive", "acct-2")
    assert one["secret"] == "sealed-cred-1" and two["secret"] == "sealed-cred-2"
    assert one["account"] == {"realm": "google", "subject": "sub-1"} and one["kind"] == "gmail"


async def test_twice_equals_once(script):
    db = EvalDb()
    await _seed(db, {"user-alex:cred-1": _old("user-alex", "cred-1")}, {"user-alex": {"account_id": "acct-1"}})
    await _run(script, db, apply=True)
    before = (await _stored(db, "user-alex:cred-1").get()).to_dict()
    second = await _run(script, db, apply=True)
    assert (second["integration_set"], second["account_id_set"], second["complete"]) == (0, 0, 1)
    assert (await _stored(db, "user-alex:cred-1").get()).to_dict() == before


async def test_unknown_kind_and_missing_profile_are_counted_not_written(script):
    db = EvalDb()
    await _seed(db, {"user-alex:cred-1": _old("user-alex", "cred-1", kind="mystery"),
                     "user-clara:cred-2": _old("user-clara", "cred-2")},
                {"user-alex": {"account_id": "acct-1"}})
    report = await _run(script, db, apply=True)
    assert report["unknown_kind"] == 1 and report["no_profile"] == 1
    assert report["unknown_kind_ids"] == ["user-ale"] and report["no_profile_users"] == ["user-cla"]
    unknown = (await _stored(db, "user-alex:cred-1").get()).to_dict()
    nameless = (await _stored(db, "user-clara:cred-2").get()).to_dict()
    assert "integration" not in unknown and unknown["account_id"] == "acct-1"   # the other field is still set
    assert nameless["integration"] == "mail" and "account_id" not in nameless
    text = script.format_report(report, apply=True)
    assert "user-alex" not in text and "user-clara" not in text and "cred-1" not in text


async def test_a_complete_record_is_left_alone(script):
    db = EvalDb()
    await _seed(db, {"user-alex:cred-1": _old("user-alex", "cred-1", integration="mail", account_id="acct-9")},
                {"user-alex": {"account_id": "acct-1"}})
    report = await _run(script, db, apply=True)
    assert report["complete"] == 1
    assert (await _stored(db, "user-alex:cred-1").get()).to_dict()["account_id"] == "acct-9"


async def test_the_store_queries_need_no_composite_index():
    """Every query shape of the store (list_for_user with/without kind, list_keys, list_by_account, list_user_ids) is
    equality-only, unordered, and the index file has no single-field exemption on the collection: automatic
    single-field indexes serve them, so no composite index is declared for `credentials`."""
    declared = json.loads(_INDEXES.read_text())
    assert not [i for i in declared if str(i.get("collectionGroup", "")).endswith("credentials")]
    db = EvalDb(indexes=_INDEXES)
    col = db.collection("credentials")
    await col.document("user-alex:cred-1").set(_old("user-alex", "cred-1", integration="mail", account_id="acct-1"))
    from google.cloud.firestore import FieldFilter
    await col.where(filter=FieldFilter("user_id", "==", "user-alex")).get()
    await col.where(filter=FieldFilter("user_id", "==", "user-alex")).where(
        filter=FieldFilter("kind", "==", "gmail")).get()
    await col.where(filter=FieldFilter("account.realm", "==", "google")).where(
        filter=FieldFilter("account.subject", "==", "sub-1")).get()
    await col.where(filter=FieldFilter("kind", "==", "gmail")).get()

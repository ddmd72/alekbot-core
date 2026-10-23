"""The Facts S3 deploy-step script that stamps `stale_since` on legacy stale facts, run on the evaluating fake."""
import copy
import json
from datetime import datetime, timezone

import pytest

from scripts.migration import fact_s3_stamp_legacy_stale as stamp_script
from tests.support.firestore_eval import EvalDb, EvalDocRef

COL = "eval_domain_facts"
NOW = datetime(2026, 10, 25, 9, 30, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ACCOUNT = "acc-12345678-secret-tail"
SECRET_TEXT = "legacy stale fact text that must stay out of stdout"


def _fact(state="stale", **extra):
    doc = {
        "account_id": ACCOUNT, "state": state, "is_current": True, "valid_to": None, "text": SECRET_TEXT,
        "version": 1, "last_updated": OLD, "created_at": OLD, "vector": [0.1, 0.2], "tags": ["a"],
    }
    doc.update(extra)
    return doc


@pytest.fixture
async def db():
    fake = EvalDb()
    col = fake.collection(COL)
    await col.document("stale-legacy-1").set(_fact())
    await col.document("stale-legacy-2").set(_fact(lifecycle_events=[
        {"at": OLD, "from": "current", "to": "stale", "actor": "hand", "reason": "earlier"}]))
    await col.document("stale-stamped").set(_fact(stale_since=SINCE))
    await col.document("current-1").set(_fact(state="current"))
    await col.document("archived-1").set(_fact(state="archived", valid_to=OLD))
    return fake


def _snapshot(db):
    col = db.collection(COL)
    return copy.deepcopy(col.docs), dict(col.versions)


async def test_dry_run_writes_nothing_and_reports_to_the_file(db, tmp_path, capsys):
    before = _snapshot(db)

    counts = await stamp_script.run(db, COL, apply=False, out_dir=tmp_path, now=NOW)

    assert _snapshot(db) == before
    assert counts == {"candidates": 2, "already_stamped": 1, "stamped": 0, "skipped_changed": 0, "failed": 0}
    report = json.loads((tmp_path / "fact_s3_legacy_stale_2026-10-25.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in report["facts"]}
    assert set(by_id) == {"stale-legacy-1", "stale-legacy-2"}
    assert by_id["stale-legacy-1"] == {
        "id": "stale-legacy-1", "account_prefix": ACCOUNT[:8], "last_updated": OLD.isoformat(), "text": SECRET_TEXT}
    out = capsys.readouterr().out
    assert "candidates: 2" in out and "already_stamped: 1" in out
    for private in ("stale-legacy-1", "stale-legacy-2", SECRET_TEXT, ACCOUNT[:8]):
        assert private not in out


async def test_apply_writes_exactly_stale_since_and_one_event(db, tmp_path):
    before_docs, _ = _snapshot(db)

    counts = await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["stamped"] == 2 and counts["skipped_changed"] == 0 and counts["failed"] == 0
    docs = db.collection(COL).docs
    for doc_id in ("stale-legacy-1", "stale-legacy-2"):
        after = dict(docs[doc_id])
        assert after.pop("stale_since") == NOW
        events = after.pop("lifecycle_events")
        expected = {k: v for k, v in before_docs[doc_id].items() if k != "lifecycle_events"}
        assert after == expected   # last_updated, state, text, vectors and every other field unchanged
        assert events[-1] == {"at": NOW, "from": "stale", "to": "stale", "actor": "migration",
                              "reason": "legacy stale, stamped at S3"}
        assert len(events) == len(before_docs[doc_id].get("lifecycle_events", [])) + 1
    assert docs["stale-legacy-2"]["lifecycle_events"][0]["reason"] == "earlier"


async def test_apply_leaves_stamped_and_non_stale_facts_alone(db, tmp_path):
    untouched = ("stale-stamped", "current-1", "archived-1")
    docs_before, versions_before = _snapshot(db)

    await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs, versions = _snapshot(db)
    for doc_id in untouched:
        assert docs[doc_id] == docs_before[doc_id]
        assert versions[doc_id] == versions_before[doc_id]
    assert docs["stale-stamped"]["stale_since"] == SINCE


async def test_a_document_changed_after_the_scan_is_skipped_and_counted(db, tmp_path, monkeypatch):
    real_scan = stamp_script.scan

    async def scan_then_interfere(database, collection):
        result = await real_scan(database, collection)
        await database.collection(COL).document("stale-legacy-1").update({"text": "edited meanwhile"})
        return result

    monkeypatch.setattr(stamp_script, "scan", scan_then_interfere)

    counts = await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs = db.collection(COL).docs
    assert counts["stamped"] == 1 and counts["skipped_changed"] == 1 and counts["failed"] == 0
    assert "stale_since" not in docs["stale-legacy-1"] and "lifecycle_events" not in docs["stale-legacy-1"]
    assert docs["stale-legacy-1"]["text"] == "edited meanwhile"
    assert docs["stale-legacy-2"]["stale_since"] == NOW


async def test_a_document_deleted_after_the_scan_is_skipped_and_counted(db, tmp_path, monkeypatch):
    real_scan = stamp_script.scan

    async def scan_then_delete(database, collection):
        result = await real_scan(database, collection)
        await database.collection(COL).document("stale-legacy-2").delete()
        return result

    monkeypatch.setattr(stamp_script, "scan", scan_then_delete)

    counts = await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["stamped"] == 1 and counts["skipped_changed"] == 1
    assert "stale-legacy-2" not in db.collection(COL).docs


async def test_a_second_apply_stamps_nothing(db, tmp_path):
    await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)
    docs_before, versions_before = _snapshot(db)

    counts = await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=datetime(2026, 10, 26, tzinfo=timezone.utc))

    assert counts == {"candidates": 0, "already_stamped": 3, "stamped": 0, "skipped_changed": 0, "failed": 0}
    assert _snapshot(db) == (docs_before, versions_before)


async def test_apply_report_lists_ids_and_outcomes_without_text(db, tmp_path):
    await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    raw = (tmp_path / "fact_s3_legacy_stale_2026-10-25_applied.json").read_text(encoding="utf-8")
    assert json.loads(raw)["outcomes"] == {"stale-legacy-1": "stamped", "stale-legacy-2": "stamped"}
    assert SECRET_TEXT not in raw


async def test_the_stamp_keeps_only_the_newest_twenty_events(tmp_path):
    fake = EvalDb()
    events = [{"at": OLD, "from": "current", "to": "stale", "actor": "hand", "reason": f"r{i}"} for i in range(20)]
    await fake.collection(COL).document("busy").set(_fact(lifecycle_events=events))

    await stamp_script.run(fake, COL, apply=True, out_dir=tmp_path, now=NOW)

    stored = fake.collection(COL).docs["busy"]["lifecycle_events"]
    assert len(stored) == 20 and stored[0]["reason"] == "r1" and stored[-1]["actor"] == "migration"


async def test_a_general_write_error_counts_as_failed_and_writes_nothing(db, tmp_path, monkeypatch, capsys):
    real_update = EvalDocRef.update

    async def update(self, data, option=None):
        if self.id == "stale-legacy-1":
            raise RuntimeError(SECRET_TEXT)
        return await real_update(self, data, option)

    monkeypatch.setattr(EvalDocRef, "update", update)
    docs_before, versions_before = _snapshot(db)

    counts = await stamp_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs, versions = _snapshot(db)
    assert counts["failed"] == 1 and counts["stamped"] == 1 and counts["skipped_changed"] == 0
    assert docs["stale-legacy-1"] == docs_before["stale-legacy-1"]
    assert versions["stale-legacy-1"] == versions_before["stale-legacy-1"]
    assert docs["stale-legacy-2"]["stale_since"] == NOW
    report = json.loads((tmp_path / "fact_s3_legacy_stale_2026-10-25_applied.json").read_text(encoding="utf-8"))
    assert report["outcomes"] == {"stale-legacy-1": "failed", "stale-legacy-2": "stamped"}
    out = capsys.readouterr().out
    assert "failed: 1" in out and "RuntimeError" in out and SECRET_TEXT not in out


def _main_with_fake_client(monkeypatch, tmp_path, argv):
    """`main()` over the fake: the real facts collection name, the report folder in tmp_path, no live client."""
    import scripts._firestore as client_helper
    from src.config.environment import EnvironmentConfig

    collection = EnvironmentConfig().domain_facts_collection
    fake = EvalDb()
    for name in (collection, "some_other_collection"):
        col = fake.collection(name)
        col.docs["stale-1"] = _fact()
        col.versions["stale-1"] = 1
    monkeypatch.setattr(client_helper, "async_client", lambda: fake)
    monkeypatch.setattr(stamp_script, "MEMORY_DIR", tmp_path)
    assert stamp_script.main(argv) == 0
    return fake, collection


def test_main_is_a_dry_run_by_default_on_the_facts_collection(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, [])

    assert "stale_since" not in fake.collection(collection).docs["stale-1"]
    report = next(tmp_path.glob("fact_s3_legacy_stale_*.json"))
    assert [f["id"] for f in json.loads(report.read_text(encoding="utf-8"))["facts"]] == ["stale-1"]
    assert not list(tmp_path.glob("*_applied.json"))


def test_main_apply_stamps_the_facts_collection_only(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, ["--apply"])

    assert "stale_since" in fake.collection(collection).docs["stale-1"]
    assert "stale_since" not in fake.collection("some_other_collection").docs["stale-1"]
    assert list(tmp_path.glob("*_applied.json"))

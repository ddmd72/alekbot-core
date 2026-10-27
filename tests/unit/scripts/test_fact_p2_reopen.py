"""The Facts P2 reopen script (`--ids-file` / `--all-archived`), run on the evaluating fake."""
import copy
import json
from datetime import datetime, timezone

import pytest

from scripts.migration import fact_p2_reopen_archives as reopen_script
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import basis_reason
from tests.support.firestore_eval import EvalDb, EvalDocRef

COL = "eval_domain_facts"
NOW = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
OLD = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ARCHIVED_AT = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
ACCOUNT = "acc-hamburg-0001-secret-tail"
OTHER = "acc-pottery-0002"
TEXT = "Alex will give a talk at the Hamburg conference on 15 October 2026."
DATE_PASSED = basis_reason("archive", ReviewBasis("date_passed", on=datetime(2026, 10, 15).date()), datetime(2026, 10, 15).date())


def _event(reason, actor="review"):
    return {"at": ARCHIVED_AT, "from": "stale", "to": "archived", "actor": actor, "reason": reason}


def _fact(account=ACCOUNT, state="archived", **extra):
    doc = {
        "account_id": account, "state": state, "is_current": state != "superseded", "valid_to": ARCHIVED_AT,
        "stale_since": SINCE, "text": TEXT, "domain": "work", "type": "state", "reported_date": OLD, "version": 1,
        "last_updated": ARCHIVED_AT, "vector": [0.1, 0.2], "expiry_kind": "horizon", "expires_at": OLD,
        "lifecycle_events": [_event(DATE_PASSED)],
    }
    doc.update(extra)
    return doc


@pytest.fixture
async def db():
    fake = EvalDb()
    col = fake.collection(COL)
    await col.document("talk").set(_fact())
    await col.document("owner-archive").set(_fact(account=OTHER, lifecycle_events=[_event("ended q=q1 (button)", "owner")]))
    await col.document("no-stale-since").set(_fact(stale_since=None))
    await col.document("current-1").set(_fact(state="current", valid_to=None))
    await col.document("system-1").set(_fact(account="system"))
    return fake


def _snapshot(db):
    col = db.collection(COL)
    return copy.deepcopy(col.docs), dict(col.versions)


ALL = {"talk", "owner-archive", "no-stale-since"}


async def test_dry_run_writes_nothing_and_reports_to_the_file(db, tmp_path, capsys):
    before = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=False, out_dir=tmp_path, now=NOW)

    assert _snapshot(db) == before
    assert counts == {"candidates": 3, "not_found": 0, "not_archived": 0, "system_account": 1,
                      "reopened": 0, "skipped_changed": 0, "failed": 0}
    report = json.loads((tmp_path / "fact_p2_reopen_2026-10-28.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in report["facts"]}
    assert set(by_id) == ALL
    assert by_id["talk"] == {
        "id": "talk", "account_prefix": ACCOUNT[:8], "text": TEXT, "domain": "work", "type": "state",
        "reported_date": OLD.isoformat(), "valid_to": ARCHIVED_AT.isoformat(), "stale_since": SINCE.isoformat(),
        "last_event": {"at": ARCHIVED_AT.isoformat(), "from": "stale", "to": "archived", "actor": "review",
                       "reason": DATE_PASSED},
    }
    out = capsys.readouterr().out
    assert "candidates: 3" in out
    for private in ("talk", TEXT, ACCOUNT[:8], OTHER[:8]):
        assert private not in out.replace("Mode:", "")


async def test_apply_writes_exactly_the_reopen(db, tmp_path):
    before_docs, _ = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["reopened"] == 3 and counts["skipped_changed"] == 0 and counts["failed"] == 0
    docs = db.collection(COL).docs
    for doc_id in ALL:
        after = dict(docs[doc_id])
        events = after.pop("lifecycle_events")
        for changed in ("state", "is_current", "valid_to", "last_updated", "stale_since"):
            after.pop(changed)
        expected = {k: v for k, v in before_docs[doc_id].items()
                    if k not in ("lifecycle_events", "state", "is_current", "valid_to", "last_updated", "stale_since")}
        assert after == expected   # text, vectors, expiry and every other field unchanged
        assert docs[doc_id]["state"] == "stale" and docs[doc_id]["is_current"] is True
        assert docs[doc_id]["valid_to"] is None and docs[doc_id]["last_updated"] == NOW
        assert events[-1] == {"at": NOW, "from": "archived", "to": "stale", "actor": "migration",
                              "reason": "reopened under P2 revised"}
        assert events[:-1] == before_docs[doc_id]["lifecycle_events"]


async def test_the_stored_stale_since_is_restored_and_a_missing_one_takes_valid_to(db, tmp_path):
    await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs = db.collection(COL).docs
    assert docs["talk"]["stale_since"] == SINCE
    assert docs["no-stale-since"]["stale_since"] == ARCHIVED_AT   # the moment its validity closed


def test_a_fact_with_neither_stale_since_nor_valid_to_takes_its_last_event_then_now():
    with_event = {"lifecycle_events": [_event("x")]}
    assert reopen_script.restored_stale_since(with_event, NOW) == ARCHIVED_AT
    assert reopen_script.restored_stale_since({}, NOW) == NOW


async def test_apply_leaves_other_states_and_the_system_account_alone(db, tmp_path):
    untouched = ("current-1", "system-1")
    docs_before, versions_before = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs, versions = _snapshot(db)
    for doc_id in untouched:
        assert docs[doc_id] == docs_before[doc_id] and versions[doc_id] == versions_before[doc_id]
    assert counts["candidates"] == 3 and counts["system_account"] == 1


async def test_account_narrows_all_archived(db, tmp_path):
    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW, account=OTHER)

    docs = db.collection(COL).docs
    assert counts["reopened"] == 1
    assert docs["owner-archive"]["state"] == "stale" and docs["talk"]["state"] == "archived"


async def test_ids_select_only_those_facts_and_report_what_is_left_out(db, tmp_path):
    counts = await reopen_script.run(
        db, COL, apply=True, out_dir=tmp_path, now=NOW,
        ids=["talk", "current-1", "gone", "system-1"])

    docs = db.collection(COL).docs
    assert counts == {"candidates": 1, "not_found": 1, "not_archived": 1, "system_account": 1,
                      "reopened": 1, "skipped_changed": 0, "failed": 0}
    assert docs["talk"]["state"] == "stale" and docs["owner-archive"]["state"] == "archived"
    assert docs["current-1"]["state"] == "current" and docs["system-1"]["state"] == "archived"


async def test_an_ids_dry_run_lists_the_left_out_ids_in_the_report(db, tmp_path):
    await reopen_script.run(db, COL, apply=False, out_dir=tmp_path, now=NOW, ids=["talk", "gone"])

    report = json.loads((tmp_path / "fact_p2_reopen_2026-10-28.json").read_text(encoding="utf-8"))
    assert [f["id"] for f in report["facts"]] == ["talk"]
    assert report["left_out"]["not_found"] == ["gone"]


async def test_a_document_changed_after_the_scan_is_skipped_and_counted(db, tmp_path, monkeypatch):
    real_scan = reopen_script.scan

    async def scan_then_interfere(database, collection, **kw):
        result = await real_scan(database, collection, **kw)
        await database.collection(COL).document("talk").update({"text": "edited meanwhile"})
        return result

    monkeypatch.setattr(reopen_script, "scan", scan_then_interfere)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs = db.collection(COL).docs
    assert counts["reopened"] == 2 and counts["skipped_changed"] == 1 and counts["failed"] == 0
    assert docs["talk"]["state"] == "archived" and docs["talk"]["text"] == "edited meanwhile"
    report = json.loads((tmp_path / "fact_p2_reopen_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert report["outcomes"]["talk"] == "skipped_changed"


async def test_a_document_deleted_after_the_scan_is_skipped(db, tmp_path, monkeypatch):
    real_scan = reopen_script.scan

    async def scan_then_delete(database, collection, **kw):
        result = await real_scan(database, collection, **kw)
        await database.collection(COL).document("talk").delete()
        return result

    monkeypatch.setattr(reopen_script, "scan", scan_then_delete)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["reopened"] == 2 and counts["skipped_changed"] == 1


async def test_a_second_apply_reopens_nothing(db, tmp_path):
    await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)
    before = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=datetime(2026, 10, 29, tzinfo=timezone.utc))

    assert counts["candidates"] == 0 and counts["reopened"] == 0
    assert _snapshot(db) == before


async def test_apply_report_lists_ids_and_outcomes_without_text(db, tmp_path):
    await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    raw = (tmp_path / "fact_p2_reopen_2026-10-28_applied.json").read_text(encoding="utf-8")
    assert json.loads(raw)["outcomes"] == {"talk": "reopened", "owner-archive": "reopened", "no-stale-since": "reopened"}
    assert TEXT not in raw


async def test_a_general_write_error_counts_as_failed_and_writes_nothing(db, tmp_path, monkeypatch, capsys):
    real_update = EvalDocRef.update

    async def update(self, data, option=None):
        if self.id == "talk":
            raise RuntimeError(TEXT)
        return await real_update(self, data, option)

    monkeypatch.setattr(EvalDocRef, "update", update)
    docs_before, versions_before = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs, versions = _snapshot(db)
    assert counts["failed"] == 1 and counts["reopened"] == 2
    assert docs["talk"] == docs_before["talk"] and versions["talk"] == versions_before["talk"]
    out = capsys.readouterr().out
    assert "RuntimeError" in out and TEXT not in out


def test_read_ids_skips_comments_and_blanks_and_dedups(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("a\n\n# note\nb  # tail\na\n", encoding="utf-8")

    assert reopen_script.read_ids(path, memory_dir=tmp_path) == ["a", "b"]


def test_read_ids_refuses_a_file_outside_the_memory_folder(tmp_path):
    outside = tmp_path / "ids.txt"
    outside.write_text("a\n", encoding="utf-8")

    with pytest.raises(reopen_script.SelectionError):
        reopen_script.read_ids(outside, memory_dir=tmp_path / "memory")


def test_last_entry_of_a_fact_without_a_trail_is_none():
    assert reopen_script.last_entry({}) is None


def _main_with_fake_client(monkeypatch, tmp_path, argv):
    import scripts._firestore as client_helper
    from src.config.environment import EnvironmentConfig

    collection = EnvironmentConfig().domain_facts_collection
    fake = EvalDb()
    for name in (collection, "some_other_collection"):
        col = fake.collection(name)
        col.docs["talk"] = _fact()
        col.versions["talk"] = 1
    monkeypatch.setattr(client_helper, "async_client", lambda: fake)
    monkeypatch.setattr(reopen_script, "MEMORY_DIR", tmp_path)
    assert reopen_script.main(argv) == 0
    return fake, collection


def test_main_all_archived_is_a_dry_run_by_default(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, ["--all-archived"])

    assert fake.collection(collection).docs["talk"]["state"] == "archived"
    assert not list(tmp_path.glob("*_applied.json")) and list(tmp_path.glob("fact_p2_reopen_*.json"))


def test_main_apply_reopens_the_facts_collection_only(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, ["--all-archived", "--apply"])

    assert fake.collection(collection).docs["talk"]["state"] == "stale"
    assert fake.collection("some_other_collection").docs["talk"]["state"] == "archived"


def test_main_reads_an_ids_file_under_the_memory_folder(monkeypatch, tmp_path):
    (tmp_path / "ids.txt").write_text("talk\n", encoding="utf-8")
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, ["--ids-file", str(tmp_path / "ids.txt"), "--apply"])

    assert fake.collection(collection).docs["talk"]["state"] == "stale"


def test_main_needs_one_selection_and_account_goes_with_all_archived(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        reopen_script.main([])
    with pytest.raises(SystemExit):
        reopen_script.main(["--ids-file", str(tmp_path / "x.txt"), "--account", "a"])
    with pytest.raises(SystemExit):
        reopen_script.main(["--ids-file", str(tmp_path / "missing.txt")])


# ------------------------------------------------------------------------------------------------ --expiry-plan

PLAN_EXPIRES = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)


def _plan(*ids):
    from scripts.consolidation.plan_inputs import ExpiryKind
    return {i: {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": PLAN_EXPIRES} for i in ids}


async def test_the_plans_expiry_is_written_with_the_reopen_and_an_absent_fact_is_listed(db, tmp_path, capsys):
    before = _snapshot(db)[0]

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW, expiry_plan=_plan("talk"))

    docs = db.collection(COL).docs
    assert counts["reopened"] == 3 and counts["without_expiry"] == 2
    assert (docs["talk"]["expiry_kind"], docs["talk"]["ttl_days"], docs["talk"]["expires_at"]) == ("date", None, PLAN_EXPIRES)
    assert docs["talk"]["state"] == "stale" and docs["talk"]["lifecycle_events"][-1]["actor"] == "migration"
    for absent in ("owner-archive", "no-stale-since"):
        assert docs[absent]["expiry_kind"] == "horizon" and docs[absent]["expires_at"] == before[absent]["expires_at"]
    report = json.loads((tmp_path / "fact_p2_reopen_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert sorted(report["without_expiry"]) == ["no-stale-since", "owner-archive"]
    assert "without_expiry: 2" in capsys.readouterr().out


async def test_a_dry_run_with_a_plan_writes_nothing_and_lists_the_facts_without_expiry(db, tmp_path):
    before = _snapshot(db)

    await reopen_script.run(db, COL, apply=False, out_dir=tmp_path, now=NOW, expiry_plan=_plan("talk"))

    assert _snapshot(db) == before
    report = json.loads((tmp_path / "fact_p2_reopen_2026-10-28.json").read_text(encoding="utf-8"))
    assert sorted(report["without_expiry"]) == ["no-stale-since", "owner-archive"]


async def test_without_a_plan_the_expiry_fields_stay_and_no_list_is_made(db, tmp_path):
    before_docs, _ = _snapshot(db)

    counts = await reopen_script.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert "without_expiry" not in counts
    assert db.collection(COL).docs["talk"]["expires_at"] == before_docs["talk"]["expires_at"]
    assert "without_expiry" not in json.loads((tmp_path / "fact_p2_reopen_2026-10-28_applied.json").read_text(encoding="utf-8"))


def test_main_reads_the_expiry_plan_from_the_memory_folder(monkeypatch, tmp_path):
    from scripts.consolidation import plan_inputs
    (tmp_path / "plan.json").write_text(json.dumps({
        "kind": plan_inputs.PLAN_KIND, "version": plan_inputs.PLAN_VERSION, "decisions": [{
            "fact_id": "talk", "fields": {"expiry_kind": "date", "ttl_days": None, "expires_at": PLAN_EXPIRES.isoformat()}}],
    }), encoding="utf-8")
    fake, collection = _main_with_fake_client(
        monkeypatch, tmp_path, ["--all-archived", "--expiry-plan", str(tmp_path / "plan.json"), "--apply"])

    stored = fake.collection(collection).docs["talk"]
    assert stored["expiry_kind"] == "date" and stored["expires_at"] == PLAN_EXPIRES and stored["state"] == "stale"


def test_main_refuses_a_plan_outside_the_memory_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(reopen_script, "MEMORY_DIR", tmp_path / "memory")
    outside = tmp_path / "plan.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit):
        reopen_script.main(["--all-archived", "--expiry-plan", str(outside)])

"""The owner's decisions on closed facts: reopen, then the typed write as the owner. Real adapter, repository and write
service over the evaluating fake; the reopen is the reopen script's own function."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.migration import fact_p2_owner_decisions as decisions_script
from scripts.migration import fact_p2_reopen_archives as reopen_script
from src.domain.entities import ContextPriority, ExpiryKind, FactState
from src.domain.fact_review_reasons import is_revive_reason, is_rewrite_reason, owner_decided_reason
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import FACTS, FactStore
from tests.support.firestore_eval import EvalDocRef

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
ARCHIVED_AT = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 1, tzinfo=timezone.utc)
TALK = "Alex will give a talk at the Hamburg conference on 15 October 2026."
HISTORY = "Took place as planned (the owner confirmed in October 2026): Alex gave a talk at the Hamburg conference."


def _closed(text, state="archived", **over):
    return make_fact(
        text, state=FactState(state), valid_to=ARCHIVED_AT, stale_since=SINCE, expiry_kind=ExpiryKind.DATE,
        expires_at=SINCE, context_priority=ContextPriority.HIGH, metadata={"place": "Hamburg"},
        lifecycle_events=[{"at": ARCHIVED_AT, "from": "stale", "to": state, "actor": "review", "reason": "archived: date passed 2026-10-15"}],
        **over,
    )


@pytest.fixture
async def store():
    s = FactStore()
    for fact in (
        _closed(TALK, id="talk"),
        _closed("Alex has an open parking fine", "invalidated", id="fine"),
        _closed("Alex trains at the Iron Temple gym", id="gym"),
        _closed("Alex takes a pottery class", id="class"),
        _closed("Alex pays a phone bill every month", "invalidated", id="phone"),
    ):
        await s.repo.add_fact(fact)
    await s.repo.add_fact(make_fact("Alex rents a pottery studio", id="studio"))
    await s.repo.add_fact(make_fact("Alex owns a bicycle", id="bike"))
    return s


def _d(fact_id, action, **extra):
    return {"fact_id": fact_id, "account_id": ACC, "action": action, **extra}


def _parse(*items):
    return decisions_script.parse_decisions(list(items))


def _current_of(store, fact_id):
    lineage = store.docs[fact_id]["lineage_id"]
    (doc,) = [d for d in store.docs.values() if d["lineage_id"] == lineage and d["state"] == "current"]
    return doc


def _snapshot(store):
    col = store.db.collection(FACTS)
    return copy.deepcopy(col.docs), dict(col.versions)


async def _run(store, tmp_path, *items, apply=True, now=NOW):
    return await decisions_script.run(
        store.db, FACTS, store.adapter, _parse(*items), apply, tmp_path, now)


# ------------------------------------------------------------------------------------------------ the reason

def test_the_owner_decided_reason_is_a_rewrite_and_never_a_revive():
    reason = owner_decided_reason()

    assert reason == "owner decided on review"
    assert is_rewrite_reason(reason) and not is_revive_reason(reason)


# ------------------------------------------------------------------------------------------------ every action

async def test_history_writes_the_text_as_a_history_version(store, tmp_path):
    counts = await _run(store, tmp_path, _d("talk", "history", text=HISTORY))

    assert counts["applied"] == 1 and counts["failed"] == 0
    new = _current_of(store, "talk")
    assert new["text"] == HISTORY and new["id"] != "talk"
    assert (new["expiry_kind"], new["context_priority"]) == ("none", "archival")
    assert not new["metadata"] and new.get("context") is None
    old = store.docs["talk"]
    assert old["state"] == "superseded" and old["superseded_by"] == new["id"]
    assert old["lifecycle_events"][-2]["reason"] == "reopened under P2 revised"
    assert old["lifecycle_events"][-2]["from"] == "archived"
    assert (new["lifecycle_events"][-1]["actor"], new["lifecycle_events"][-1]["reason"]) == ("owner", "owner decided on review")


async def test_current_writes_the_text_with_expiry_none_at_its_stored_priority(store, tmp_path):
    text = "Alex gave a talk at the Hamburg conference on 15 October 2026."

    await _run(store, tmp_path, _d("talk", "current", text=text))

    new = _current_of(store, "talk")
    assert new["text"] == text and new["expiry_kind"] == "none" and new["context_priority"] == "high"
    assert new["metadata"] == {"place": "Hamburg"}


async def test_revive_makes_it_current_with_the_text_unchanged_and_expiry_none(store, tmp_path):
    await _run(store, tmp_path, _d("gym", "revive"))

    doc = store.docs["gym"]
    assert (doc["state"], doc["text"], doc["expiry_kind"], doc["valid_to"]) == ("current", "Alex trains at the Iron Temple gym", "none", None)
    assert doc["lifecycle_events"][-1]["reason"] == "owner decided on review"


async def test_invalidate_closes_a_reopened_fact(store, tmp_path):
    await _run(store, tmp_path, _d("fine", "invalidate"))

    doc = store.docs["fine"]
    assert doc["state"] == "invalidated" and doc["lifecycle_events"][-2]["from"] == "invalidated"
    assert doc["lifecycle_events"][-1]["reason"] == "owner decided on review"


async def test_supersede_names_a_stored_current_successor_or_one_written_earlier(store, tmp_path):
    counts = await _run(
        store, tmp_path,
        _d("class", "supersede", successor_id="studio"),
        _d("talk", "history", text=HISTORY),
        _d("phone", "supersede", successor_id="talk"),   # talk is written earlier in this run
    )

    assert counts["applied"] == 3
    assert store.docs["class"]["state"] == "superseded" and store.docs["class"]["superseded_by"] == "studio"
    assert store.docs["phone"]["superseded_by"] == _current_of(store, "talk")["id"]   # the chain's current version


# ------------------------------------------------------------------------------------------------ the dry run

async def test_a_dry_run_writes_nothing_and_reports_old_and_new_text(store, tmp_path, capsys):
    before = _snapshot(store)

    counts = await _run(
        store, tmp_path, _d("talk", "history", text=HISTORY), _d("gym", "revive"), apply=False)

    assert _snapshot(store) == before
    assert counts["to_write"] == 2 and counts["applied"] == 0
    report = json.loads((tmp_path / "fact_p2_owner_decisions_2026-10-28.json").read_text(encoding="utf-8"))
    by_id = {f["id"]: f for f in report["facts"]}
    assert by_id["talk"] == {"id": "talk", "action": "history", "account_prefix": ACC[:8], "state": "archived",
                             "old_text": TALK, "new_text": HISTORY, "successor_id": None, "resumes": False}
    assert by_id["gym"]["new_text"] == by_id["gym"]["old_text"]
    out = capsys.readouterr().out
    assert "to_write: 2" in out and TALK not in out and "talk" not in out.replace("Mode:", "")


# ------------------------------------------------------------------------------------------------ refusals

@pytest.mark.parametrize("items, why", [
    ([], "non-empty"),
    ("x", "non-empty"),
    (["x"], "not an object"),
    ([{"fact_id": "a", "account_id": ACC}], "action is missing"),
    ([_d("a", "burn")], "unknown action"),
    ([_d("a", "history")], "text"),
    ([_d("a", "current", text="")], "text"),
    ([_d("a", "history", text="two\nlines")], "one line"),
    ([_d("a", "current", text=" ".join(["word"] * 41))], "words"),
    ([_d("a", "supersede")], "successor_id"),
    ([_d("a", "supersede", successor_id="a")], "successor_id"),
    ([_d("a", "revive"), _d("a", "invalidate")], "listed twice"),
])
def test_a_malformed_file_is_refused_as_a_whole(items, why):
    with pytest.raises(decisions_script.DecisionsError, match=why):
        decisions_script.parse_decisions(items)


async def test_a_successor_that_is_not_current_or_written_earlier_refuses_the_file(store, tmp_path):
    before = _snapshot(store)
    for successor in ("missing", "gym", "bike-not-there"):   # gym is archived, the others do not exist
        with pytest.raises(decisions_script.DecisionsError, match="successor"):
            await _run(store, tmp_path, _d("class", "supersede", successor_id=successor))
    with pytest.raises(decisions_script.DecisionsError, match="successor"):
        await _run(store, tmp_path, _d("class", "supersede", successor_id="talk"), _d("talk", "revive"))   # written later

    assert _snapshot(store) == before
    assert not list(tmp_path.glob("*.json"))


async def test_a_successor_of_another_account_is_refused(store, tmp_path):
    other = make_fact("Alex rents a studio elsewhere", id="elsewhere", account="acc-other")
    await store.repo.add_fact(other)

    with pytest.raises(decisions_script.DecisionsError, match="successor"):
        await _run(store, tmp_path, _d("class", "supersede", successor_id="elsewhere"))


async def test_facts_that_are_missing_in_another_account_system_or_not_closed_are_left_alone(store, tmp_path):
    before = _snapshot(store)

    counts = await _run(
        store, tmp_path,
        _d("gone", "revive"), {**_d("gym", "revive"), "account_id": "acc-other"}, _d("bike", "revive"),
    )

    assert counts["not_found"] == 1 and counts["wrong_account"] == 1 and counts["not_closed"] == 1
    assert counts["to_write"] == 0 and _snapshot(store) == before


async def test_a_system_account_fact_is_left_alone(store, tmp_path):
    await store.repo.add_fact(_closed("A system instruction", id="sys", account="system"))

    counts = await decisions_script.run(
        store.db, FACTS, store.adapter, [decisions_script.Decision("sys", "system", "revive")], True, tmp_path, NOW)

    assert counts["system_account"] == 0 and counts["wrong_account"] == 0 or counts["to_write"] == 0
    assert store.docs["sys"]["state"] == "archived"


def test_the_decisions_file_must_sit_under_the_memory_folder(tmp_path):
    outside = tmp_path / "d.json"
    outside.write_text(json.dumps([_d("a", "revive")]), encoding="utf-8")

    with pytest.raises(Exception, match="must be under"):
        decisions_script.load_decisions(outside, tmp_path / "memory")
    assert decisions_script.load_decisions(outside, tmp_path)[0].fact_id == "a"
    outside.write_text("{", encoding="utf-8")
    with pytest.raises(decisions_script.DecisionsError, match="not JSON"):
        decisions_script.load_decisions(outside, tmp_path)


# ------------------------------------------------------------------------------------------------ changed, failed, again

async def test_a_fact_changed_after_the_scan_is_skipped_and_listed(store, tmp_path, monkeypatch):
    real_scan = decisions_script.scan

    async def scan_then_interfere(db, collection, decisions):
        result = await real_scan(db, collection, decisions)
        await db.collection(FACTS).document("gym").update({"text": "edited meanwhile"})
        return result

    monkeypatch.setattr(decisions_script, "scan", scan_then_interfere)

    counts = await _run(store, tmp_path, _d("gym", "revive"), _d("fine", "invalidate"))

    assert counts["skipped_changed"] == 1 and counts["applied"] == 1
    assert store.docs["gym"]["state"] == "archived" and store.docs["gym"]["text"] == "edited meanwhile"
    report = json.loads((tmp_path / "fact_p2_owner_decisions_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert report["outcomes"] == {"gym": "skipped_changed", "fine": "applied"} and report["details"]["gym"] == "reopen"


async def test_a_second_apply_writes_nothing(store, tmp_path):
    items = (
        _d("talk", "history", text=HISTORY), _d("gym", "revive"), _d("fine", "invalidate"),
        _d("class", "supersede", successor_id="studio"), _d("phone", "current", text="Alex pays a phone bill monthly."),
    )
    await _run(store, tmp_path, *items)
    before = _snapshot(store)

    counts = await _run(store, tmp_path, *items, now=NOW + timedelta(days=1))

    assert counts["already_decided"] + counts["not_closed"] == 5 and counts["to_write"] == 0 and counts["applied"] == 0
    assert _snapshot(store) == before


async def test_an_action_that_fails_leaves_the_fact_reopened_and_the_next_run_resumes_it(store, tmp_path, monkeypatch):
    real = store.adapter.apply_fact_update

    async def refuse(fact_id, update, account_id, **kw):
        if fact_id == "gym":
            return FactUpdateOutcome.refused(fact_id, UpdateRefusal.INVALID, "refused for the test")
        return await real(fact_id, update, account_id, **kw)

    monkeypatch.setattr(store.adapter, "apply_fact_update", refuse)
    counts = await _run(store, tmp_path, _d("gym", "revive"), _d("fine", "invalidate"))

    assert counts["failed"] == 1 and counts["applied"] == 1
    assert store.docs["gym"]["state"] == "stale" and store.docs["gym"]["lifecycle_events"][-1]["reason"] == "reopened under P2 revised"
    report = json.loads((tmp_path / "fact_p2_owner_decisions_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert report["details"]["gym"] == "invalid"

    monkeypatch.setattr(store.adapter, "apply_fact_update", real)
    again = await _run(store, tmp_path, _d("gym", "revive"), _d("fine", "invalidate"), now=NOW + timedelta(days=1))

    assert again["resumed"] == 1 and again["applied"] == 1 and again["already_decided"] == 1
    events = [e["reason"] for e in store.docs["gym"]["lifecycle_events"]]
    assert events.count("reopened under P2 revised") == 1 and events[-1] == "owner decided on review"
    assert store.docs["gym"]["state"] == "current"


async def test_an_exception_never_stops_the_run(store, tmp_path, monkeypatch, capsys):
    real = store.adapter.apply_fact_update

    async def explode(fact_id, update, account_id, **kw):
        if fact_id == "gym":
            raise RuntimeError(TALK)
        return await real(fact_id, update, account_id, **kw)

    monkeypatch.setattr(store.adapter, "apply_fact_update", explode)

    counts = await _run(store, tmp_path, _d("gym", "revive"), _d("fine", "invalidate"))

    assert counts["failed"] == 1 and counts["applied"] == 1
    report = json.loads((tmp_path / "fact_p2_owner_decisions_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert report["details"]["gym"] == "RuntimeError" and TALK not in json.dumps(report)


async def test_a_conflict_of_the_action_is_skipped_not_failed(store, tmp_path, monkeypatch):
    async def conflict(fact_id, update, account_id, **kw):
        return FactUpdateOutcome.refused(fact_id, UpdateRefusal.CONFLICT, "conflict")

    monkeypatch.setattr(store.adapter, "apply_fact_update", conflict)

    counts = await _run(store, tmp_path, _d("gym", "revive"))

    assert counts["skipped_changed"] == 1 and counts["failed"] == 0


async def test_a_reopen_that_fails_is_a_failure_and_the_action_is_not_tried(store, tmp_path, monkeypatch):
    real_update = EvalDocRef.update

    async def update(self, data, option=None):
        if self.id == "gym":
            raise RuntimeError("x")
        return await real_update(self, data, option)

    monkeypatch.setattr(EvalDocRef, "update", update)

    counts = await _run(store, tmp_path, _d("gym", "revive"))

    assert counts["failed"] == 1 and store.docs["gym"]["state"] == "archived"


# ------------------------------------------------------------------------------------------------ main

def _main_world(monkeypatch, tmp_path, store, items):
    """`main()` over the store's fake database: the real facts collection name holds copies of the seeded documents."""
    import scripts._firestore as client_helper
    from src.config.environment import EnvironmentConfig

    collection = EnvironmentConfig().domain_facts_collection
    real = store.db.collection(collection)
    real.docs.update(copy.deepcopy(store.docs))
    real.versions.update(store.db.collection(FACTS).versions)
    monkeypatch.setattr(client_helper, "async_client", lambda: store.db)
    monkeypatch.setattr(decisions_script, "build_port", lambda db: store.adapter)
    monkeypatch.setattr(decisions_script, "MEMORY_DIR", tmp_path)
    (tmp_path / "d.json").write_text(json.dumps(items), encoding="utf-8")
    return real


def test_main_is_a_dry_run_by_default(monkeypatch, tmp_path, store):
    real = _main_world(monkeypatch, tmp_path, store, [_d("gym", "revive")])

    assert decisions_script.main(["--decisions", str(tmp_path / "d.json")]) == 0

    assert real.docs["gym"]["state"] == "archived"
    assert list(tmp_path.glob("fact_p2_owner_decisions_*.json")) and not list(tmp_path.glob("*_applied.json"))


def test_main_refuses_a_bad_file_and_a_missing_one(monkeypatch, tmp_path, store):
    _main_world(monkeypatch, tmp_path, store, [_d("a", "burn")])

    with pytest.raises(SystemExit):
        decisions_script.main(["--decisions", str(tmp_path / "d.json")])
    with pytest.raises(SystemExit):
        decisions_script.main(["--decisions", str(tmp_path / "missing.json")])


def test_main_refuses_a_file_whose_successor_is_missing(monkeypatch, tmp_path, store):
    real = _main_world(monkeypatch, tmp_path, store, [_d("class", "supersede", successor_id="nowhere")])

    with pytest.raises(SystemExit):
        decisions_script.main(["--decisions", str(tmp_path / "d.json"), "--apply"])
    assert real.docs["class"]["state"] == "archived" and not list(tmp_path.glob("fact_p2_*.json"))


async def test_the_action_carries_the_listed_fact_as_expected_id_and_a_moved_chain_is_skipped(store, tmp_path, monkeypatch):
    seen = {}
    real = store.adapter.apply_fact_update

    async def watch(fact_id, update, account_id, **kw):
        seen[fact_id] = kw.get("expected_id")
        return await real(fact_id, update, account_id, **kw)

    monkeypatch.setattr(store.adapter, "apply_fact_update", watch)
    await _run(store, tmp_path, _d("gym", "revive"))
    assert seen == {"gym": "gym"}

    async def moved(fact_id, update, account_id, **kw):
        return FactUpdateOutcome.refused(fact_id, UpdateRefusal.MOVED, "moved on")

    monkeypatch.setattr(store.adapter, "apply_fact_update", moved)
    counts = await _run(store, tmp_path, _d("fine", "invalidate"))

    assert counts["skipped_changed"] == 1 and counts["failed"] == 0
    report = json.loads((tmp_path / "fact_p2_owner_decisions_2026-10-28_applied.json").read_text(encoding="utf-8"))
    assert report["details"] == {"fine": "moved"}

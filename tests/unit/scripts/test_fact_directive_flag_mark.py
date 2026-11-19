"""The PR 13 deploy-step script that marks the old-domain directives with the `directive` flag, run on the evaluating
fake (FACT_PROFILE_RFC §9 row 13)."""
import copy

import pytest

from scripts.migration import fact_directive_flag_mark as mark_script
from tests.support.firestore_eval import EvalDb

COL = "eval_domain_facts"
ACC = "acc-own-0000"
OTHER = "acc-other-0000"
ACCOUNTS = {"DEV_ACCOUNT_ID": ACC, "SECOND_ACCOUNT_ID": OTHER}


def _fact(account=ACC, domain="agent_directive", state="current", **extra):
    doc = {"account_id": account, "state": state, "text": "suggest only vegetarian recipes", "type": "event", "version": 1}
    if domain is not None:
        doc["domain"] = domain
    doc.update(extra)
    return doc


@pytest.fixture
async def db():
    fake = EvalDb()
    col = fake.collection(COL)
    await col.document("own-current").set(_fact())
    await col.document("own-superseded").set(_fact(state="superseded", version=1))
    await col.document("own-upper").set(_fact(domain="AGENT_DIRECTIVE", state="invalidated"))
    await col.document("own-marked").set(_fact(directive=True))
    await col.document("own-sport").set(_fact(domain="preference", text="plays tennis on Sundays"))
    await col.document("own-no-domain").set(_fact(domain=None, text="morning yoga 30 min"))
    await col.document("other-current").set(_fact(account=OTHER, text="answer in English"))
    await col.document("other-sport").set(_fact(account=OTHER, domain="biographical", text="Alex Doe"))
    return fake


def _docs(db):
    return copy.deepcopy(db.collection(COL).docs)


def _flagged(db):
    return sorted(doc_id for doc_id, doc in db.collection(COL).docs.items() if doc.get("directive") is True)


def _never(count):
    raise AssertionError("a dry run never asks")


async def test_the_dry_run_counts_per_account_and_writes_nothing(db, capsys):
    before = _docs(db)

    counts = await mark_script.run(db, COL, ACCOUNTS, apply=False, confirm=_never)

    assert _docs(db) == before
    assert counts == {
        "DEV_ACCOUNT_ID": {"to_mark": 3, "already_marked": 1, "marked": 0},
        "SECOND_ACCOUNT_ID": {"to_mark": 1, "already_marked": 0, "marked": 0},
    }
    out = capsys.readouterr().out
    assert "DEV_ACCOUNT_ID: to_mark 3, already_marked 1, marked 0" in out
    assert ACC not in out and OTHER not in out and "suggest only vegetarian recipes" not in out


async def test_apply_marks_only_the_old_directive_documents_of_every_state_and_writes_only_the_flag(db):
    before = _docs(db)

    counts = await mark_script.run(db, COL, ACCOUNTS, apply=True, confirm=lambda count: count == 4)

    assert counts["DEV_ACCOUNT_ID"]["marked"] == 3 and counts["SECOND_ACCOUNT_ID"]["marked"] == 1
    assert _flagged(db) == ["other-current", "own-current", "own-marked", "own-superseded", "own-upper"]
    after = _docs(db)
    for doc_id in ("own-current", "own-superseded", "own-upper", "other-current"):
        assert after[doc_id] == {**before[doc_id], "directive": True}
    for doc_id in ("own-marked", "own-sport", "own-no-domain", "other-sport"):
        assert after[doc_id] == before[doc_id]


async def test_a_second_apply_marks_nothing(db):
    await mark_script.run(db, COL, ACCOUNTS, apply=True, confirm=lambda count: True)
    after_first = _docs(db)

    counts = await mark_script.run(db, COL, ACCOUNTS, apply=True, confirm=_never)

    assert _docs(db) == after_first
    assert counts["DEV_ACCOUNT_ID"] == {"to_mark": 0, "already_marked": 4, "marked": 0}
    assert counts["SECOND_ACCOUNT_ID"] == {"to_mark": 0, "already_marked": 1, "marked": 0}


async def test_apply_without_the_typed_confirmation_writes_nothing(db, capsys):
    before = _docs(db)
    asked = []

    counts = await mark_script.run(db, COL, ACCOUNTS, apply=True, confirm=lambda count: asked.append(count) or False)

    assert asked == [4]
    assert _docs(db) == before
    assert all(c["marked"] == 0 for c in counts.values())
    assert "not confirmed: nothing written" in capsys.readouterr().out


def test_the_typed_confirmation_is_the_count_and_a_non_interactive_shell_aborts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "4")
    assert mark_script.confirm_count(4) is True
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert mark_script.confirm_count(4) is False

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert mark_script.confirm_count(4) is False


async def test_writes_go_in_batches_below_the_firestore_limit(db, monkeypatch):
    monkeypatch.setattr(mark_script, "BATCH_SIZE", 2)

    await mark_script.run(db, COL, ACCOUNTS, apply=True, confirm=lambda count: True)

    assert [len(commit) for commit in db.commits] == [2, 1, 1]


def test_the_accounts_come_from_env_keys_and_a_missing_one_is_named():
    assert mark_script.account_ids(["DEV_ACCOUNT_ID"], {"DEV_ACCOUNT_ID": ACC}) == {"DEV_ACCOUNT_ID": ACC}
    with pytest.raises(SystemExit, match="SECOND_ACCOUNT_ID"):
        mark_script.account_ids(["DEV_ACCOUNT_ID", "SECOND_ACCOUNT_ID"], {"DEV_ACCOUNT_ID": ACC})

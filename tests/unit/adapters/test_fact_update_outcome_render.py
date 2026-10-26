"""The typed update outcome (facts S4 · T8e, T3c review M2): code reads `FactUpdateOutcome`, and the model's dict is its
render (`update_result`), byte for byte the dict the adapter returned at 05103e3, before the outcome was typed (the
write path is unchanged between 05103e3 and the commit before this one; the pins passed there first).

Each case runs on a fresh store through the path that reaches it: the model's `update_fact`, or code's
`apply_fact_update` rendered by `update_result` (history, the typed refusals). The JSON text of the result, keys in
order, equals the text pinned from 05103e3. A random new id (a version, a history fact) is compared as `<new>`.
"""
import json
from datetime import timedelta

import pytest

from src.domain.entities import FactState
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_tool_result import update_result
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from src.domain.fact_update_outcome import EXPIRY_REFUSALS, FactUpdateOutcome, UpdateRefusal
from src.domain.fact_write_context import NO_READABLE_SOURCE_NOTE
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon

NEW = "<new>"
SEEDED = {"cur", "other", "old", "new", "stale", "gone", "missing"}
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")


def model_dict(outcome):
    assert isinstance(outcome, FactUpdateOutcome)
    return update_result(outcome)


def seed(store):
    common = dict(state="current", is_current=True, reported_date=T0, expiry_kind="horizon", ttl_days=30,
                  expires_at=T0 + timedelta(days=400), tags=["work", "consolidated"], lifecycle_events=[])
    put_raw(store, "cur", "works at Acme", **common)
    put_raw(store, "other", "works at Acme Corp", **common)
    put_raw(store, "old", "worked at Acme", **{**common, "state": "superseded", "is_current": False,
                                              "superseded_by": "new"}, entity={"lineage_id": "lin-x"})
    put_raw(store, "new", "works at Acme now", **common, entity={"lineage_id": "lin-x", "version": 2})
    put_raw(store, "stale", "works at Acme stale", **{**common, "state": "stale", "stale_since": T0 + timedelta(days=40)})
    put_raw(store, "gone", "worked at Gone", **{**common, "state": "superseded", "is_current": False,
                                                "superseded_by": "nowhere"}, entity={"lineage_id": "lin-g"})


def fresh():
    store = FactStore(indexes=INDEXES_FILE)
    seed(store)
    return store


def text_of(result):
    """The result's JSON text, keys in their order, with a new random id named `<new>`."""
    text = json.dumps(result)
    for value in (result.get("fact_id"), result.get("history_fact_id")):
        if isinstance(value, str) and value not in SEEDED:
            text = text.replace(value, NEW)
    return text


async def by_model(store, fact_id, updates):
    return await store.adapter.update_fact(fact_id, updates, ACC, context=ctx("stage1"))


async def by_code(store, fact_id, update, actor="owner", **extra):
    return model_dict(await store.adapter.apply_fact_update(fact_id, update, ACC, context=ctx(actor), **extra))


ARCHIVE = ParsedFactUpdate(state=FactState.ARCHIVED)
EVIDENCE = T0 + timedelta(days=50)

# name -> (how the case runs, its JSON text at 05103e3)
CASES = {
    "written_in_place": (
        lambda s: by_model(s, "cur", {"state": "archived"}),
        '{"fact_id": "cur", "status": "updated", "version": 1, "message": "Fact updated successfully"}',
    ),
    "a_new_version": (
        lambda s: by_model(s, "cur", {"content": "works at Beta", "expiry": horizon(3650)}),
        '{"fact_id": "<new>", "status": "updated", "version": 2, "message": "Fact updated successfully"}',
    ),
    "followed_forward": (
        lambda s: by_model(s, "old", {"state": "archived"}),
        '{"fact_id": "new", "status": "updated", "version": 2, "message": "Fact updated successfully; fact old was '
        'replaced earlier, so the update was applied to its current version new", "resolved_from": "old"}',
    ),
    "a_version_followed_forward": (
        lambda s: by_model(s, "old", {"content": "works at Beta", "expiry": horizon(3650)}),
        '{"fact_id": "<new>", "status": "updated", "version": 3, "message": "Fact updated successfully; fact old was '
        'replaced earlier, so the update was applied to its current version new", "resolved_from": "old"}',
    ),
    "a_note": (
        lambda s: by_model(s, "cur", {"state": "invalidated", "expiry": horizon(3)}),
        '{"fact_id": "cur", "status": "updated", "version": 1, "message": "Fact updated successfully", '
        '"note": "expiry is not used when a fact is closed; ignored"}',
    ),
    "a_history_fact": (
        lambda s: by_code(s, "stale", ARCHIVE, evidence_at=EVIDENCE, reason=ENDED, history=HISTORY),
        '{"fact_id": "stale", "status": "updated", "version": 1, "message": "Fact archived; history fact <new> created '
        'beside it", "history_fact_id": "<new>"}',
    ),
    "not_found": (
        lambda s: by_model(s, "missing", {"state": "archived"}),
        '{"fact_id": "missing", "status": "not_found", "message": "Fact not found"}',
    ),
    "an_invalid_expiry": (
        lambda s: by_model(s, "cur", {"expiry": {"kind": "bogus"}}),
        '{"fact_id": "cur", "status": "failed", "refused": "expiry", "message": "Invalid expiry: kind \'bogus\' is '
        'unknown. Use one of {kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}. Nothing was '
        'written. Resend this call with a valid expiry before retiring any fact it replaces."}',
    ),
    "an_invalid_argument": (
        lambda s: by_model(s, "cur", {"content": ""}),
        '{"fact_id": "cur", "status": "failed", "message": "Error updating fact: content must be a non-empty string; '
        'leave the key out to keep the current text"}',
    ),
    "an_invalid_typed_update": (
        lambda s: by_code(s, "cur", ParsedFactUpdate(state=FactState.STALE), actor="stage1"),
        '{"fact_id": "cur", "status": "failed", "message": "only the system marks a fact stale; to retire a fact use '
        'ARCHIVED, INVALIDATED or SUPERSEDED"}',
    ),
    "a_history_refusal": (
        lambda s: by_code(s, "cur", ARCHIVE, actor="stage1", evidence_at=EVIDENCE, reason=ENDED, history=HISTORY),
        '{"fact_id": "cur", "status": "failed", "message": "history: only the review or the owner write a history '
        'fact, not stage1; nothing was written"}',
    ),
    "an_unresolved_chain": (
        lambda s: by_model(s, "gone", {"state": "archived"}),
        '{"fact_id": "gone", "status": "failed", "message": "fact gone was superseded and its successor is unknown; '
        'search again"}',
    ),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_the_model_dict_is_the_one_05103e3_returned(name):
    run, pinned = CASES[name]

    result = await run(fresh())

    assert text_of(result) == pinned


async def test_a_conflict_twice_is_the_dict_05103e3_returned(monkeypatch):
    store = fresh()

    async def conflicted(stored, fields):
        raise FactWriteConflict(stored.fact.id)

    monkeypatch.setattr(store.repo, "update_fact_fields", conflicted)

    result = await by_model(store, "cur", {"state": "archived"})

    assert text_of(result) == '{"fact_id": "cur", "status": "failed", "message": "fact cur changed concurrently; search again"}'



async def test_a_born_due_refusal_is_the_dict_05103e3_returned():
    # The message names a day count from today, so the keys in order and the opening are pinned, not the whole text.
    result = await by_model(fresh(), "cur", {"state": "current", "expiry": horizon(30)})

    assert list(result) == ["fact_id", "status", "refused", "born_due", "message"]
    assert text_of({k: v for k, v in result.items() if k != "message"}) == (
        '{"fact_id": "cur", "status": "failed", "refused": "expiry", "born_due": true}'
    )
    assert result["message"].startswith(
        "counted from when the owner said it (2026-01-01), a horizon of 30 days ended 2026-01-31: give at least "
    )


# ------------------------------------------------------------------------------------------------ the typed outcome

async def test_code_reads_a_written_outcome_without_parsing_a_dict():
    store = fresh()

    outcome = await store.adapter.apply_fact_update(
        "old", parse_fact_update({"content": "works at Beta", "expiry": horizon(3650)}), ACC, context=ctx("stage1"),
    )

    assert outcome.written and outcome.refusal is None and outcome.refusal_text is None
    assert outcome.fact_id == "old" and outcome.current_id == "new" and outcome.resolved_from == "old"
    assert outcome.written_id not in SEEDED and outcome.version == 3 and outcome.history_fact_id is None
    assert store.docs[outcome.written_id]["text"] == "works at Beta"


async def test_a_history_outcome_names_the_archived_and_the_new_fact():
    store = fresh()

    outcome = await store.adapter.apply_fact_update(
        "stale", ARCHIVE, ACC, context=ctx("owner"), evidence_at=EVIDENCE, reason=ENDED, history=HISTORY,
    )

    assert outcome.written and outcome.written_id == "stale" and outcome.resolved_from is None
    assert store.docs[outcome.history_fact_id]["text"] == HISTORY and store.docs["stale"]["state"] == "archived"


@pytest.mark.parametrize("fact_id, update, actor, extra, refusal", [
    ("missing", ARCHIVE, "stage1", {}, UpdateRefusal.NOT_FOUND),
    ("cur", ParsedFactUpdate(state=FactState.STALE), "stage1", {}, UpdateRefusal.INVALID),
    ("cur", ARCHIVE, "stage1", {"evidence_at": EVIDENCE, "reason": ENDED, "history": HISTORY}, UpdateRefusal.HISTORY),
    ("gone", ARCHIVE, "stage1", {}, UpdateRefusal.UNRESOLVED),
    ("cur", ParsedFactUpdate(state=FactState.CURRENT, expiry=parse_fact_update(
        {"expiry": horizon(30)}).expiry), "stage1", {}, UpdateRefusal.BORN_DUE),
])
async def test_each_refusal_has_its_kind_and_writes_nothing(fact_id, update, actor, extra, refusal):
    store = fresh()
    before = {k: dict(v) for k, v in store.docs.items()}

    outcome = await store.adapter.apply_fact_update(fact_id, update, ACC, context=ctx(actor), **extra)

    assert not outcome.written and outcome.refusal is refusal and outcome.refusal_text
    assert outcome.written_id is None and outcome.resolved_from is None and outcome.notes == ()
    assert store.docs == before


async def test_a_conflict_twice_is_its_own_refusal(monkeypatch):
    store = fresh()

    async def conflicted(stored, fields):
        raise FactWriteConflict(stored.fact.id)

    monkeypatch.setattr(store.repo, "update_fact_fields", conflicted)

    outcome = await store.adapter.apply_fact_update("cur", ARCHIVE, ACC, context=ctx("owner"), reason=ENDED,
                                                    evidence_at=EVIDENCE)

    assert outcome.refusal is UpdateRefusal.CONFLICT and outcome.refusal_text == "fact cur changed concurrently; search again"


# ------------------------------------------------------------------------------------------------ the renderer

def test_the_renderer_writes_the_notes_joined():
    outcome = FactUpdateOutcome(fact_id="a", written_id="a", version=1, current_id="a",
                                notes=(NO_READABLE_SOURCE_NOTE, "another note"))

    assert json.dumps(update_result(outcome)) == json.dumps({
        "fact_id": "a", "status": "updated", "version": 1, "message": "Fact updated successfully",
        "note": f"{NO_READABLE_SOURCE_NOTE}; another note",
    })
    assert NO_READABLE_SOURCE_NOTE in outcome.notes


def test_the_renderer_puts_history_notes_and_the_forwarded_id_in_their_order():
    outcome = FactUpdateOutcome(fact_id="a", written_id="b", version=2, current_id="b", history_fact_id="h",
                                notes=("n",))

    assert list(update_result(outcome).items()) == [
        ("fact_id", "b"), ("status", "updated"), ("version", 2),
        ("message", "Fact archived; history fact h created beside it; fact a was replaced earlier, so the update was "
                    "applied to its current version b"),
        ("history_fact_id", "h"), ("note", "n"), ("resolved_from", "a"),
    ]


@pytest.mark.parametrize("refusal", list(UpdateRefusal))
def test_every_refusal_renders_as_a_failed_dict_with_its_text(refusal):
    rendered = update_result(FactUpdateOutcome.refused("a", refusal, "why"))

    status = "not_found" if refusal is UpdateRefusal.NOT_FOUND else "failed"
    tags = {"expiry": {"refused": "expiry"}, "born_due": {"refused": "expiry", "born_due": True}}.get(refusal.value, {})
    assert json.dumps(rendered) == json.dumps({"fact_id": "a", "status": status, **tags, "message": "why"})
    assert (refusal in EXPIRY_REFUSALS) == bool(tags)


@pytest.mark.parametrize("fields", [
    {"written_id": "a", "version": 1},                                            # no current id
    {"written_id": "a", "version": 1, "current_id": "a", "refusal_text": "x"},    # a written one with a refusal text
    {"refusal": UpdateRefusal.ERROR},                                             # a refusal without its text
    {"refusal": UpdateRefusal.ERROR, "refusal_text": "x", "written_id": "a"},     # a refusal that wrote
    {"refusal": UpdateRefusal.ERROR, "refusal_text": "x", "notes": ("n",)},       # a refusal with notes
])
def test_an_outcome_that_contradicts_itself_is_refused(fields):
    with pytest.raises(ValueError):
        FactUpdateOutcome(fact_id="a", **fields)

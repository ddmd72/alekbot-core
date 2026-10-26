"""The model's `update_fact` after the typed write path was split off (facts S4 · T3c, inward review L1): it is
`parse_fact_update` + the one apply, and its results are byte-identical to before the split.

Each scenario runs twice on fresh twin stores: once through `update_fact` (the model's dict), once through
`apply_fact_update(parse_fact_update(updates))`. Both results equal the result pinned from the adapter at 5a83780 (before
T3c), and the stored documents end the same. A version's new id is random, so it is compared as `<version>`.

Reviewer edit (S4 T8e): `apply_fact_update` returns a typed outcome (T3c review M2); these assertions read its model
dict through the one renderer (`tests.support.fact_write.rendered`), so each keeps its meaning.
"""
from datetime import datetime, timedelta

import pytest

from src.domain.fact_update import parse_fact_update
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon, rendered

NEW_VERSION = "<version>"
_UPDATED = {"message": "Fact updated successfully", "status": "updated"}

# (fact id, updates, the result update_fact returned at 5a83780)
SCENARIOS = {
    "revive": ("cur", {"state": "current", "expiry": horizon(3650)}, {"fact_id": "cur", "version": 1, **_UPDATED}),
    "rewrite": (
        "cur", {"content": "works at Beta", "expiry": horizon(3650)}, {"fact_id": NEW_VERSION, "version": 2, **_UPDATED},
    ),
    "archive": ("cur", {"state": "archived"}, {"fact_id": "cur", "version": 1, **_UPDATED}),
    "invalidate": (
        "cur", {"state": "invalidated", "expiry": horizon(3)},
        {"fact_id": "cur", "version": 1, "note": "expiry is not used when a fact is closed; ignored", **_UPDATED},
    ),
    "supersede": (
        "cur", {"state": "superseded", "superseded_by": "other"}, {"fact_id": "cur", "version": 1, **_UPDATED},
    ),
    "reported_date_only": (
        "cur", {"reported_date": "2026-01-05T10:00:00+00:00"}, {"fact_id": "cur", "version": 1, **_UPDATED},
    ),
    "followed_forward": (
        "old", {"state": "archived"},
        {
            "fact_id": "new", "version": 2, "resolved_from": "old", "status": "updated",
            "message": "Fact updated successfully; fact old was replaced earlier, so the update was applied to its "
                       "current version new",
        },
    ),
}

# Refused by the parser: only the model path has one, so these are pinned on update_fact alone.
PARSE_REFUSED = {
    "invalid_expiry": (
        "cur", {"expiry": {"kind": "bogus"}},
        {
            "fact_id": "cur", "status": "failed", "refused": "expiry",
            "message": "Invalid expiry: kind 'bogus' is unknown. Use one of {kind: date, date: YYYY-MM-DD}, "
                       "{kind: horizon, days: N} or {kind: none}. Nothing was written. Resend this call with a valid "
                       "expiry before retiring any fact it replaces.",
        },
    ),
    "invalid": (
        "cur", {"content": ""},
        {
            "fact_id": "cur", "status": "failed",
            "message": "Error updating fact: content must be a non-empty string; leave the key out to keep the current text",
        },
    ),
    # A missing fact is reported before an invalid argument, as before the split.
    "invalid_on_missing": ("missing", {"content": ""}, {"fact_id": "missing", "message": "Fact not found", "status": "not_found"}),
}

SEEDED = {"cur", "other", "old", "new"}
_TIMES = ("last_updated", "valid_to", "valid_from", "created_at", "expires_at", "at")


def seed(store):
    common = dict(state="current", is_current=True, reported_date=T0, expiry_kind="horizon", ttl_days=30,
                  expires_at=T0 + timedelta(days=400), tags=["work", "consolidated"], lifecycle_events=[])
    put_raw(store, "cur", "works at Acme", **common)
    put_raw(store, "other", "works at Acme Corp", **common)
    put_raw(store, "old", "worked at Acme", **{**common, "state": "superseded", "is_current": False,
                                              "superseded_by": "new"}, entity={"lineage_id": "lin-x"})
    put_raw(store, "new", "works at Acme now", **common, entity={"lineage_id": "lin-x", "version": 2})


def twin():
    store = FactStore(indexes=INDEXES_FILE)
    seed(store)
    return store


def normalised(result, fact_id):
    out = dict(result)
    if out.get("fact_id") not in (fact_id, "new"):
        out["fact_id"] = NEW_VERSION
    return out


def comparable(docs):
    """The stored documents with the write's own clock taken out and a new version's random id named `<version>`."""
    def scrub(value):
        if isinstance(value, dict):
            return {k: ("<t>" if k in _TIMES and isinstance(v, datetime) else scrub(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value
    new_ids = [doc_id for doc_id in docs if doc_id not in SEEDED]
    text = repr(sorted((NEW_VERSION if doc_id in new_ids else doc_id, repr(scrub(doc))) for doc_id, doc in docs.items()))
    for doc_id in new_ids:
        text = text.replace(doc_id, NEW_VERSION)
    return text


def test_the_adapter_is_the_typed_port_too():
    assert isinstance(FactStore().adapter, FactUpdatePort)


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_update_fact_is_parse_plus_the_typed_apply_and_returns_what_it_did_before(name):
    fact_id, updates, pinned = SCENARIOS[name]
    model, typed = twin(), twin()

    by_model = await model.adapter.update_fact(fact_id, updates, ACC, context=ctx("stage1"))
    by_code = rendered(await typed.adapter.apply_fact_update(
        fact_id, parse_fact_update(updates, "Europe/Lisbon"), ACC, context=ctx("stage1"),
    ))

    assert normalised(by_model, fact_id) == pinned
    assert normalised(by_code, fact_id) == pinned
    assert comparable(model.docs) == comparable(typed.docs)


@pytest.mark.parametrize("name", list(PARSE_REFUSED))
async def test_a_parse_refusal_is_the_result_it_was_before(name):
    fact_id, updates, pinned = PARSE_REFUSED[name]
    store = twin()
    before = {k: dict(v) for k, v in store.docs.items()}

    assert await store.adapter.update_fact(fact_id, updates, ACC, context=ctx("stage1")) == pinned
    assert store.docs == before


async def test_a_born_due_horizon_is_refused_the_same_way_on_both_paths():
    # The message names a day count from today, so its tags and opening are pinned, not the whole text.
    updates = {"state": "current", "expiry": horizon(30)}
    model, typed = twin(), twin()

    by_model = await model.adapter.update_fact("cur", updates, ACC, context=ctx("stage1"))
    by_code = rendered(await typed.adapter.apply_fact_update("cur", parse_fact_update(updates), ACC, context=ctx("stage1")))

    assert by_model == by_code
    assert {k: v for k, v in by_model.items() if k != "message"} == {
        "fact_id": "cur", "status": "failed", "refused": "expiry", "born_due": True,
    }
    assert by_model["message"].startswith(
        "counted from when the owner said it (2026-01-01), a horizon of 30 days ended 2026-01-31: give at least "
    )

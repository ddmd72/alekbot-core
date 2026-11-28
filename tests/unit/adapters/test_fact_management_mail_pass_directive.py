"""A fact the mail pass creates is never a standing directive, whatever the model sent; the conversation pass keeps
the flag as sent. Real adapter, write service and repository on the evaluating Firestore fake."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx

EXPIRY = {"kind": "none"}
ATTRIBUTES = {"account_id": ACC, "user_id": USER, "directive": True, "temporal_class": "stable",
              "context_priority": "medium", "tags": ["style"], "type": "principle", "expiry": EXPIRY}


@pytest.fixture
def store(kit_db):
    return FactStore(indexes=INDEXES_FILE, kit_db=kit_db)


@pytest.mark.parametrize("actor, stored", [("email", False), ("stage1", True)])
async def test_the_directive_flag_follows_the_pass(store, actor, stored):
    res = await store.adapter.create_fact("Suggest only vegetarian recipes.", dict(ATTRIBUTES), context=ctx(actor))
    assert res["status"] == "created", res
    assert store.docs[res["fact_id"]]["directive"] is stored


async def test_a_mail_fact_is_not_pinned_so_it_needs_an_expiry_decision(store):
    attributes = {**{k: v for k, v in ATTRIBUTES.items() if k != "expiry"}, "type": "state"}
    refused = await store.adapter.create_fact("Suggest only vegetarian recipes.", attributes, context=ctx("email"))
    assert refused["status"] != "created" and store.docs == {}
    kept = await store.adapter.create_fact("Suggest only vegetarian recipes.", attributes, context=ctx("stage1"))
    assert kept["status"] == "created"  # a directive is pinned: no expiry decision needed


async def _two_facts(store, directive_first):
    first = (await store.adapter.create_fact("Suggest only vegetarian recipes.", dict(ATTRIBUTES, directive=directive_first),
                                             context=ctx("stage1")))["fact_id"]
    second = (await store.adapter.create_fact("Answer in English.", dict(ATTRIBUTES, directive=False),
                                              context=ctx("stage1")))["fact_id"]
    return first, second


async def test_the_mail_pass_does_not_merge_a_directive_and_writes_nothing(store):
    first, second = await _two_facts(store, True)
    before = {k: dict(v) for k, v in store.docs.items()}
    res = await store.adapter.merge_facts([first, second], "Vegetarian recipes, in English.", dict(ATTRIBUTES),
                                          context=ctx("email"))
    assert res["status"] != "merged" and res["message"].startswith("Tool error:") and "conversation pass" in res["message"]
    assert store.docs == before


async def test_the_mail_pass_still_merges_plain_facts_and_the_conversation_pass_merges_directives(store):
    a, b = await _two_facts(store, False)
    assert (await store.adapter.merge_facts([a, b], "Both.", dict(ATTRIBUTES), context=ctx("email")))["status"] == "merged"
    c, d = await _two_facts(store, True)
    assert (await store.adapter.merge_facts([c, d], "Both.", dict(ATTRIBUTES), context=ctx("stage1")))["status"] == "merged"

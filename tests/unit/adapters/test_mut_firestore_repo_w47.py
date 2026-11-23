"""Mutation-survivor kill for FirestoreFactRepository.get_active_facts: `directive` must stay in the select projection."""
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore


async def test_get_active_facts_returns_the_stored_directive_flag():
    store = FactStore(indexes=INDEXES_FILE)
    await store.repo.add_fact(make_fact("rule", directive=True))
    await store.repo.add_fact(make_fact("plain"))

    by_id = {f.id: f for f in await store.repo.get_active_facts(ACC)}

    assert by_id["f-rule"].directive is True
    assert by_id["f-plain"].directive is False

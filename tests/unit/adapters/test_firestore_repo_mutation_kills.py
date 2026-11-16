"""Mutation-survivor kills for FirestoreFactRepository: the nearest-neighbour query projects `account_id` only."""
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, embed


async def test_nearest_projects_exactly_the_account_id_field():
    store = FactStore(indexes=INDEXES_FILE)
    await store.repo.add_fact(make_fact("car"))
    issued = store.db.collection("eval_domain_facts").executed

    assert await store.repo.nearest_fact_ids(ACC, "vector", embed("car"), 5) == ["f-car"]

    selects = [op[1] for op in issued[-1] if op[0] == "select"]
    assert selects == [("account_id",)]

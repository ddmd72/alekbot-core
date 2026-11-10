"""The fact search reads the people cards (fact profile RFC §5.2, §7) through the graph's one people-card store; the
instance ledger holds that store to one instance, so this is the store the profile step rebuilds."""
from src.adapters.firestore_person_card_repo import FirestorePersonCardRepository
from tests.support.prod_graph import prod_graph  # noqa: F401  (fixture)


async def test_the_fact_search_holds_the_graphs_people_card_store(prod_graph):
    assert isinstance(prod_graph.app.fact_search._person_cards, FirestorePersonCardRepository)

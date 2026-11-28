"""Facts S3 · T7: the production graph's ConsolidationService resolves the owner's zone from the graph's own stores."""
import pytest

from tests.support.prod_graph import ACCOUNT_ID, USER_ID, ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_graph_service_reads_the_owner_zone_through_the_graphs_own_repositories(prod_graph: ProdGraph):
    app = prod_graph.app
    service = app.worker_handler._consolidation
    zones = service._owner_zones
    assert zones._account_repo is app.account_repo and zones._user_repo is app.user_repo and service._user_repo is app.user_repo

    await app.account_repo.set_member_role(ACCOUNT_ID, USER_ID, "owner")
    owner = await app.user_repo.get_user(USER_ID)
    owner.config.timezone = "Europe/Lisbon"
    await app.user_repo.create_user(owner)

    assert (await zones.resolve(ACCOUNT_ID)).zone == "Europe/Lisbon"

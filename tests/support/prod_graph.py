"""The production application graph, built offline and started (P9 design §5a).

`prod_graph` is the same `build_application_graph(config, db)` that `main()` calls, over the deploy's sealed
environment (`tests/support/prod_env.py`) with fakes at the SDK boundaries only. After the build it runs
`await app.start()`, seeds account `a1` and user `u1` through the real repositories, builds user `u1`'s eager agents
and then every lazy agent of `UserAgentFactory._LAZY_BUILDERS`, so the walk sees what a served user touches.
One build per test function; `shutdown()` runs in the teardown.
"""
from typing import Any, AsyncIterator, Dict, NamedTuple

import pytest

from tests.support import prod_env

ACCOUNT_ID = "a1"
USER_ID = "u1"


class ProdGraph(NamedTuple):
    app: Any                     # ApplicationGraph
    offline: prod_env.OfflineServices
    lazy: Dict[str, bool]        # agent_type -> what create_agent_on_demand returned


async def seed(app: Any) -> None:
    from src.domain.billing import BillingAccount
    from src.domain.user import UserProfile
    await app.account_repo.create_account(BillingAccount(account_id=ACCOUNT_ID))
    await app.user_repo.create_user(UserProfile(user_id=USER_ID, account_id=ACCOUNT_ID))


async def build(monkeypatch) -> ProdGraph:
    from src.composition.application_graph import build_application_graph
    from src.composition.user_agent_factory import UserAgentFactory
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    app = build_application_graph(load_settings(), offline.db)
    await app.start()
    await seed(app)
    await app.agent_factory.ensure_agents_for_user(USER_ID)
    lazy = {}
    for agent_type in sorted(UserAgentFactory._LAZY_BUILDERS):
        lazy[agent_type] = await app.agent_factory.create_agent_on_demand(agent_type, USER_ID)
    return ProdGraph(app, offline, lazy)


@pytest.fixture
async def prod_graph(monkeypatch) -> AsyncIterator[ProdGraph]:
    graph = await build(monkeypatch)
    try:
        yield graph
    finally:
        await graph.app.shutdown()

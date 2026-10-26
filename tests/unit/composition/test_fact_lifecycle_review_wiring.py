"""Facts S4 · T9a: the production graph builds the fact janitor with the memory review's collaborators (plan §3.12)."""
import pytest

from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_janitor_holds_the_review_store_and_the_agent_task_queue(prod_graph: ProdGraph):
    app = prod_graph.app
    sweep = app.worker_handler._fact_lifecycle_sweep

    assert isinstance(sweep, FactLifecycleSweepService)
    assert sweep._review_store is not None and sweep._review_store is app.container.fact_review_store
    assert sweep._task_queue is not None and sweep._task_queue is app.agent_task_queue


async def test_the_janitor_reads_the_owner_and_the_interval_through_the_graphs_own_stores(prod_graph: ProdGraph):
    app = prod_graph.app
    sweep = app.worker_handler._fact_lifecycle_sweep

    assert sweep._account_repo is app.account_repo and sweep._user_repo is app.user_repo
    assert sweep._owner_zones is app.worker_handler._fact_review._owner_zones


async def test_the_graph_answers_the_scheduler_task_with_the_review_figures_at_zero(prod_graph: ProdGraph):
    result, status = await prod_graph.app.worker_handler.handle({"task_type": "fact_lifecycle_sweep"})

    assert status == 200
    assert (result["review_enqueued"], result["purge_overdue"], result["accounts"]) == (0, 0, 0)

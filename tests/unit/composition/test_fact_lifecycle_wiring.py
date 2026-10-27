"""Facts S3 · T10: the production graph builds the fact janitor from its own stores and answers `/worker` with it (plan §3.13)."""
import logging

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from tests.support import prod_env
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow

EMPTY_RUN = {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "deferred": 0, "conflicts": 0,
             "no_decision": 0, "breaker_tripped": 0, "review_enqueued": 0, "purge_overdue": 0, "dry_run": False}
# The deploy sets FACT_LIFECYCLE_SWEEP_DRY_RUN=0 since the owner switched the janitor on (2026-10-27, FACT_LIFECYCLE_RFC
# §7 step 5): under the sealed deploy env the sweep writes.


async def test_the_sweep_holds_the_containers_repository_counters_and_the_ops_sink(prod_graph: ProdGraph):
    app = prod_graph.app
    sweep = app.worker_handler._fact_lifecycle_sweep

    assert isinstance(sweep, FactLifecycleSweepService)
    assert sweep._repo is app.container.repository
    assert sweep._counters is app.container.fact_lifecycle_counters
    assert isinstance(sweep._counters, FirestoreFactLifecycleCounters)
    assert sweep._alert_sink is app.alert_sink and sweep._alert_sink is not None


async def test_under_the_sealed_deploy_env_the_sweep_writes(prod_graph: ProdGraph):
    assert prod_env.deploy_env()["FACT_LIFECYCLE_SWEEP_DRY_RUN"] == "0"
    assert prod_graph.app.worker_handler._fact_lifecycle_sweep._dry_run is False


async def test_the_graphs_consolidation_service_reads_the_owner_zone_through_the_account_repository(prod_graph: ProdGraph):
    app = prod_graph.app

    assert app.worker_handler._consolidation._owner_zones._account_repo is app.account_repo


async def test_the_graph_answers_the_scheduler_task_with_the_empty_run_totals(prod_graph: ProdGraph):
    result = await prod_graph.app.worker_handler.handle({"task_type": "fact_lifecycle_sweep"})

    assert result == (EMPTY_RUN, 200)


@pytest.mark.parametrize("raw, line, dry_run", [
    (None, "fact lifecycle sweep: log-only", True),
    ("0", "fact lifecycle sweep: writes", False),
])
async def test_the_knob_is_read_from_the_environment_at_build_and_the_startup_line_names_the_mode(
    monkeypatch, caplog, raw, line, dry_run
):
    from src.composition.application_graph import build_application_graph
    from src.config.settings import load_settings

    offline = prod_env.install_offline_services(monkeypatch)
    if raw is None:
        # the deploy env now sets the knob (=0): unset it, so this case still proves an unset knob is log-only
        monkeypatch.delenv("FACT_LIFECYCLE_SWEEP_DRY_RUN")
    else:
        monkeypatch.setenv("FACT_LIFECYCLE_SWEEP_DRY_RUN", raw)
    caplog.set_level(logging.INFO)

    app = build_application_graph(load_settings(), offline.db)

    assert app.worker_handler._fact_lifecycle_sweep._dry_run is dry_run
    lines = [r.getMessage() for r in caplog.records if "fact lifecycle sweep:" in r.getMessage()]
    assert lines == [line]

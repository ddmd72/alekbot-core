"""The older benches put their dry-run wrapper under both of the agent's write ports (Facts S4 T6b review L8): a typed write
never reaches the live store from them."""

from types import SimpleNamespace

import pytest

from scripts.consolidation.legacy_dry_run_ports import NoTypedWrites, install_dry_run
from src.domain.fact_update import ParsedFactUpdate
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_write import ctx


class _ModelOnlyDryRun(FactManagementPort):
    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        return []

    async def create_fact(self, content, metadata, *, context):
        return {"status": "created"}

    async def update_fact(self, fact_id, updates, account_id, *, context):
        return {"status": "updated"}

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        return {"status": "merged"}


class _BothDryRun(_ModelOnlyDryRun, FactUpdatePort):
    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None, history=None, expected_id=None):
        return {"status": "updated"}


def _live_agent():
    live = object()
    return SimpleNamespace(_fact_management=live, _fact_updates=live), live


async def test_a_model_only_dry_run_leaves_no_typed_path_to_the_live_store():
    agent, live = _live_agent()
    dry_run = _ModelOnlyDryRun()

    install_dry_run(agent, dry_run)

    assert agent._fact_management is dry_run and agent._fact_updates is not live
    assert isinstance(agent._fact_updates, NoTypedWrites)
    with pytest.raises(RuntimeError, match="--s3-kit"):
        await agent._fact_updates.apply_fact_update("f1", ParsedFactUpdate(state=None), "acc", context=ctx())


def test_a_dry_run_with_the_typed_path_takes_both_ports():
    agent, _ = _live_agent()
    dry_run = _BothDryRun()

    install_dry_run(agent, dry_run)

    assert agent._fact_management is dry_run and agent._fact_updates is dry_run

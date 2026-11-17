"""The S3 dry-run kit's one write path: the recorder wraps an adapter built on the overlay, so a create lands in the overlay and the live
store stays untouched even when every live write raises."""
from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from tests.support.fact_write import ctx
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (store, memory_dir: fixtures)
    GuardedRepo, answered, kit_agent, live_state, memory_dir, meta, store,
)


async def test_the_recorded_adapter_reads_and_writes_the_overlay_and_a_create_never_reaches_the_live_store(store, tmp_path):  # noqa: F811
    agent, kit = await kit_agent(store, tmp_path, answered(), live_repo=GuardedRepo(store.repo))
    before = live_state(store)

    result = await agent._fact_management.create_fact("walks daily", meta(), context=ctx(actor="stage1", timezone="UTC"))

    assert isinstance(kit.repo, NoWriteFactRepository) and agent._fact_management._inner._repo is kit.repo
    assert result["status"] == "created"
    assert live_state(store) == before
    assert [w["kind"] for w in kit.repo.writes] == ["add_fact"]
    assert (await kit.repo.get_fact_by_id(result["fact_id"])).text == "walks daily"

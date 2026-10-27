"""A review dry run on the kit never writes to the live store through the typed path (Facts S4 T6b, T3c re-review L6).

The typed write port became the agent's own dependency (`fact_updates`), so the kit must install its recorder as both
ports. The agent here is built with the live adapter as both, as production builds it; the review pass runs through the
agent's real loop (wrapped by the kit) with a `FactReviewScope` over the run's tracker. Added by the T6b reviewer."""
from datetime import datetime, timedelta, timezone

from scripts.consolidation import s3_dry_run_kit as kit_module
from src.agents.consolidation_agent import _TrackingFactUpdates
from src.domain.entities import FactState
from src.domain.fact_review_batch import ReviewBatch
from src.infrastructure.fact_review_scope import FactReviewScope
from src.domain.llm import ToolCall
from tests.support.fact_kit import ACC, T0, USER, put_raw
from tests.support.fact_write import ctx
from tests.unit.agents.test_consolidation_write_context import calls
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (fixtures: store, memory_dir)
    answered, kit_agent, live_state, memory_dir, metered, seed, store,
)


async def test_a_review_write_through_the_installed_kit_lands_in_the_overlay_only(store, tmp_path):
    put_raw(store, "old", "works at Acme", state="stale", stale_since=T0 + timedelta(days=40), reported_date=T0)
    evidence = await seed(store, "works at Beta since October")
    # P2 revised (P2-2): a standing fact a neighbour shows ended is superseded by it, never archived on evidence
    supersede = ToolCall(name="update_fact", args={
        "fact_id": "old", "updates": {"state": "superseded", "superseded_by": evidence},
    })
    agent, kit = await kit_agent(store, tmp_path, metered(calls(supersede)), answered())
    assert isinstance(agent._fact_updates, kit_module.WriteRecorder) and agent._fact_updates is agent._fact_management
    before = live_state(store)

    tracker = _TrackingFactUpdates(agent._fact_management, agent._fact_updates)
    batch = ReviewBatch((await agent._repo.get_fact_by_id("old"),), ())
    scope = FactReviewScope(batch, ACC, "UTC", datetime.now(timezone.utc), agent._repo, tracker)
    await agent._run_consolidation_loop(
        user_message_text="review", system_prompt="SYS", user_id=USER, account_id=ACC,
        write_context=ctx(actor="review", timezone="UTC"), pass_kind="review", review_scope=scope,
    )

    assert [(d.fact_id, d.outcome) for d in scope.decisions()] == [("old", "superseded")]
    assert live_state(store) == before
    assert kit.repo.overlay_document("old").state is FactState.SUPERSEDED
    assert [(row["action"], row["actor"], row["status"]) for row in kit.rows] == [("update", "review", "updated")]
    assert tracker.wrote

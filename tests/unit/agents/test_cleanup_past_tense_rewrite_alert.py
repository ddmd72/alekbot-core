"""T2b review H1 through the real loop: on a cleanup pass a past plan rewritten as history (other text) after the
refusal posts nothing; a dropped part next to an unrelated create still alerts."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, on
from tests.unit.agents.test_consolidation_error_turns import build
from tests.unit.agents.test_consolidation_write_context import REPORT, calls, create


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def run(agent, label):
    return await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=USER, account_id=ACC,
        write_context=ctx(actor=label, timezone="UTC"),
    )


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster"])
async def test_a_past_tense_rewrite_after_the_refusal_posts_nothing(store, label):
    agent = build(
        store,
        calls(create("Plans a trip to Lisbon on the 3rd", expiry=on("2020-01-03"))),
        calls(create("Took a trip to Lisbon on the 3rd", expiry=EXPIRY_NONE)),
        REPORT,
    )

    await run(agent, label)

    assert agent._alert_sink.posts == []
    assert [d["text"] for d in store.docs.values()] == ["Took a trip to Lisbon on the 3rd"]


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster"])
async def test_a_dropped_part_next_to_an_unrelated_create_still_alerts(store, label):
    agent = build(
        store,
        calls(create("Plans a trip to Lisbon on the 3rd", expiry=on("2020-01-03"))),
        calls(create("Prefers window seats on long flights", expiry=EXPIRY_NONE)),
        REPORT,
    )

    await run(agent, label)

    (post,) = agent._alert_sink.posts
    assert post.startswith("🚨 Consolidation left expiry refusals") and "Lisbon" not in post

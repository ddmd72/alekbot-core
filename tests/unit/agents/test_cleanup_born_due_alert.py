"""S3-D12: a cleanup pass whose born-due refusal is never answered posts the 🚨 and reports it to gate G2; the same
refusal on Stage 1 stays quiet, and an answered one never alerts. Real loop, tool executor and adapter on the fakes."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, on
from tests.unit.agents.test_consolidation_error_turns import build
from tests.unit.agents.test_consolidation_write_context import REPORT, calls, create


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def past_plan(text="the trip on the 3rd"):
    return calls(create(text, expiry=on("2020-01-01")))


async def run(agent, label):
    cleanup = label not in ("stage1", "email")
    return await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=USER, account_id=ACC, pass_label=label,
        write_context=ctx(actor=label, timezone="UTC", model_dates=not cleanup),
    )


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster"])
async def test_a_dropped_past_plan_on_a_cleanup_pass_posts_the_alert(store, label):
    agent = build(store, past_plan(), REPORT)

    await run(agent, label)

    (post,) = agent._alert_sink.posts
    assert post.startswith("🚨 Consolidation left expiry refusals") and f"pass `{label}`" in post
    assert "the trip on the 3rd" not in post
    assert not store.docs


@pytest.mark.parametrize("label", ["stage2a", "stage2b", "cluster"])
async def test_a_past_plan_rewritten_as_history_on_a_cleanup_pass_posts_nothing(store, label):
    agent = build(store, past_plan(), calls(create("the trip on the 3rd", expiry=EXPIRY_NONE)), REPORT)

    await run(agent, label)

    assert agent._alert_sink.posts == []
    assert [d["expiry_kind"] for d in store.docs.values()] == ["none"]


async def test_a_dropped_past_plan_on_stage1_posts_nothing(store):
    agent = build(store, past_plan(), REPORT)

    await run(agent, "stage1")

    assert agent._alert_sink.posts == []

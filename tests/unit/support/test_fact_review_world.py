"""The review pass's world (tests/support/fact_review_world.py, recording_task_queue.py): each fake behaves like the port it stands for."""
import asyncio
from datetime import timedelta

import pytest

from src.domain.channel_binding import ChannelBinding
from src.domain.entities import FactState
from src.domain.language import LanguageCode
from src.domain.llm import Message, MessagePart
from src.domain.ui_messages import UIMessage
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import NOW, PAST, JanitorWorld
from tests.support.fact_review_world import ReviewWorld


@pytest.fixture
def world():
    return ReviewWorld()


def pair(text):
    return [Message(role="user", parts=[MessagePart(text=text)]), Message(role="model", parts=[MessagePart(text="ok")])]


# ------------------------------------------------------------------------------------------------ session store

async def test_a_batch_append_is_stored_in_order_under_the_session(world):
    await world.session_store.append_messages_batch("u:c", pair("one"), "u", raise_on_error=True)
    await world.session_store.append_messages_batch("u:c", pair("two"), "u")
    assert [m.parts[0].text for m in await world.session_store.messages("u:c")] == ["one", "ok", "two", "ok"]
    assert await world.session_store.get_latest_session_id("u") == "u:c"
    assert await world.session_store.messages("u:other") == []


async def test_a_failed_append_raises_only_when_asked_and_stores_nothing(world):
    world.session_store.fail = True
    with pytest.raises(RuntimeError):
        await world.session_store.append_messages_batch("u:c", pair("lost"), "u", raise_on_error=True)
    await world.session_store.append_messages_batch("u:c", pair("swallowed"), "u")
    assert await world.session_store.messages("u:c") == []
    world.session_store.fail = False
    await world.session_store.append_messages_batch("u:c", pair("kept"), "u", raise_on_error=True)
    assert len(await world.session_store.messages("u:c")) == 2


async def test_the_store_keeps_a_copy_not_the_callers_message(world):
    message = Message(role="model", parts=[MessagePart(text="summary", full_text="full")])
    await world.session_store.append_messages_batch("u:c", [message], "u")
    message.parts[0].text = "edited after the append"
    [stored] = await world.session_store.messages("u:c")
    assert (stored.parts[0].text, stored.parts[0].full_text) == ("summary", "full")
    stored.parts[0].text = "edited after the load"
    assert (await world.session_store.messages("u:c"))[0].parts[0].text == "summary"


async def test_the_owner_falls_back_to_the_session_id_as_in_production(world):
    await world.session_store.append_messages_batch("u:c", pair("one"))
    assert (await world.session_store.load_session("u:c")).owner_id == "u:c"


async def test_the_latest_session_is_the_one_with_the_latest_activity(world):
    await world.session_store.append_messages_batch("u:a", pair("one"), "u")
    await world.session_store.append_messages_batch("u:b", pair("two"), "u")
    await world.session_store.append_messages_batch("u:a", pair("three"), "u")
    assert await world.session_store.get_latest_session_id("u") == "u:a"


async def test_an_overflow_over_the_window_is_cut_in_batches_and_reported(world):
    store = world.session_store
    await store.append_messages_batch("u:c", [Message(role="user", parts=[MessagePart(text=f"m{i}")]) for i in range(200)], "u")
    await store.append_messages_batch("u:c", pair("new"), "u")
    await asyncio.sleep(0)
    [(owner, session_id, batch)] = store.overflow
    assert (owner, session_id, len(batch)) == ("u", "u:c", 100)
    assert len(await store.messages("u:c")) == 102


# ----------------------------------------------------------------------------------------------- notifier, state

async def test_a_raw_notification_goes_to_the_primary_channel_then_the_last_active(world):
    await world.notification_state.save("u", "telegram", "T9")
    assert await world.notifier.notify_raw("u", "a", "to last active") is True
    await world.notification_state.save_primary("u", "slack", "C1")
    assert await world.notifier.notify_raw("u", "a", "to primary") is True
    assert await world.notifier.notify_raw("u", "a", "to override", "C7", "slack") is True
    assert world.channels.sent == [("telegram", "T9", "to last active"), ("slack", "C1", "to primary"),
                                   ("slack", "C7", "to override")]


async def test_a_raw_notification_with_no_stored_channel_is_not_delivered(world):
    assert await world.notifier.notify_raw("u", "a", "nobody home") is False
    assert world.channels.sent == []


async def test_a_raw_notification_on_an_unconfigured_platform_or_a_failing_send_is_not_delivered(world):
    await world.notification_state.save_primary("u", "slack", "C1")
    world.channels.unconfigured.add("slack")
    assert await world.notifier.notify_raw("u", "a", "no platform") is False
    world.channels.unconfigured.clear()
    world.channels.fail = True
    assert await world.notifier.notify_raw("u", "a", "send fails") is False
    world.channels.fail = False
    assert await world.notifier.notify_raw("u", "a", "back") is True
    assert world.channels.sent == [("slack", "C1", "back")]


async def test_primary_and_last_active_channels_are_kept_apart(world):
    state = world.notification_state
    assert await state.get_primary("u") is None and await state.get("u") is None
    await state.save("u", "telegram", "T9")
    await state.save_primary("u", "slack", "C1")
    primary, last = await state.get_primary("u"), await state.get("u")
    assert (primary.platform, primary.channel_id) == ("slack", "C1")
    assert (last.platform, last.channel_id) == ("telegram", "T9")


async def test_channel_bindings_save_get_and_delete(world):
    binding = ChannelBinding(channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="u")
    await world.channel_bindings.save(binding)
    assert await world.channel_bindings.get("C1") == binding
    await world.channel_bindings.delete("C1")
    await world.channel_bindings.delete("C1")
    assert await world.channel_bindings.get("C1") is None


async def test_the_language_service_ties_the_ui_language_to_the_preferred_one(world):
    assert await world.language.get_preference("u") == (None, True)
    assert await world.language.resolve_ui_language("u") == LanguageCode.EN
    world.language.account_default = LanguageCode.FR
    assert await world.language.resolve_ui_language("u") == LanguageCode.FR
    world.language.preferences["u"] = (LanguageCode.UK, False)
    assert await world.language.get_preference("u") == (LanguageCode.UK, False)
    assert await world.language.resolve_ui_language("u") == LanguageCode.UK
    assert await world.language.get_preference("v") == (None, True)
    assert await world.language.resolve_ui_language("v") == LanguageCode.FR


def test_the_localization_is_the_real_file_adapter(world):
    uk = world.localization.get_ui_string(LanguageCode.UK, UIMessage.RESPONSE_READY)
    en = world.localization.get_ui_string(LanguageCode.EN, UIMessage.RESPONSE_READY)
    assert uk and en and uk != en


# --------------------------------------------------------------------------------------------- lease, clock

async def test_the_lease_is_the_real_one_on_the_fake_database(world):
    until = world.now + timedelta(minutes=10)
    assert await world.lease.acquire(ACC, "run-1", world.now, until) is True
    assert await world.lease.acquire(ACC, "run-2", world.now, until) is False
    world.advance(minutes=11)
    assert await world.lease.acquire(ACC, "run-2", world.now, world.now + timedelta(minutes=10)) is True


def test_the_clock_moves_only_when_told_to(world):
    assert world.clock() == NOW
    assert world.advance(days=1, hours=2) == NOW + timedelta(days=1, hours=2)
    assert world.clock() == NOW + timedelta(days=1, hours=2)


# ------------------------------------------------------------------------------------------------- task queue

async def test_a_second_task_of_one_dedup_key_is_not_created(world):
    first = await world.queue.enqueue_worker_task("fact_review", {"user_id": "u", "mode": "next"}, dedup_key="fact-review-next:q1")
    again = await world.queue.enqueue_worker_task("fact_review", {"user_id": "u", "mode": "next"}, dedup_key="fact-review-next:q1")
    other = await world.queue.enqueue_worker_task("fact_review", {"user_id": "u", "mode": "next"}, dedup_key="fact-review-next:q2")
    assert first and again is None and other and other != first
    assert world.enqueued == [("fact_review", {"task_type": "fact_review", "user_id": "u", "mode": "next"})] * 2


async def test_the_same_key_under_another_task_type_is_a_different_task(world):
    assert await world.queue.enqueue_worker_task("fact_review", {}, dedup_key="k")
    assert await world.queue.enqueue_worker_task("fact_lifecycle_sweep", {}, dedup_key="k")
    assert [kind for kind, _ in world.enqueued] == ["fact_review", "fact_lifecycle_sweep"]


async def test_tasks_without_a_key_are_never_deduplicated(world):
    await world.queue.enqueue_worker_task("fact_review", {"n": 1})
    await world.queue.enqueue_worker_task("fact_review", {"n": 1})
    assert len(world.enqueued) == 2


async def test_each_world_has_its_own_queue(world):
    other = ReviewWorld()
    await world.queue.enqueue_worker_task("fact_review", {}, dedup_key="k")
    assert await other.queue.enqueue_worker_task("fact_review", {}, dedup_key="k")
    assert other.enqueued != [] and len(world.enqueued) == 1


async def test_concurrent_enqueues_of_one_key_create_one_task(world):
    results = await asyncio.gather(*[world.queue.enqueue_worker_task("fact_review", {}, dedup_key="k") for _ in range(50)])
    assert len([r for r in results if r]) == 1 and len(world.enqueued) == 1


async def test_the_janitor_world_has_the_same_queue_on_first_use():
    janitor = JanitorWorld()
    assert await janitor.queue.enqueue_worker_task("fact_review", {"user_id": USER}, dedup_key="k")
    assert await janitor.queue.enqueue_worker_task("fact_review", {"user_id": USER}, dedup_key="k") is None
    assert janitor.tasks.worker_tasks() == [("fact_review", {"task_type": "fact_review", "user_id": USER})]


# ------------------------------------------------------------------------------------------------ the facts

async def test_a_seeded_stale_fact_is_stale_since_now_and_in_the_backlog(world):
    fact_id = await world.seed_stale("old plan")
    fresh = await world.seed("still fine")
    stored = world.docs[fact_id]
    assert stored["state"] == FactState.STALE.value and stored["stale_since"] == NOW
    assert world.docs[fresh]["state"] == FactState.CURRENT.value
    assert await world.repo.stale_backlog() == {ACC: (1, NOW)}


async def test_staling_refreshes_the_biographical_cache_built_before(world):
    await world.seed("kept fact")
    gone = await world.seed("old plan", expires_at=PAST)
    await world.repo.refresh_biographical_context_cache(ACC)
    assert {item["text"] for item in await world.repo.get_biographical_context_cached(ACC)} == {"kept fact", "old plan"}
    await world.stale(gone)
    assert {item["text"] for item in await world.repo.get_biographical_context_cached(ACC)} == {"kept fact"}


async def test_seeding_a_stale_fact_that_is_not_due_says_so(world):
    with pytest.raises(AssertionError, match="not due"):
        await world.seed_stale("future plan", expires_at=NOW + timedelta(days=5))
    with pytest.raises(AssertionError, match="not due"):
        await world.stale("no-such-fact")


async def test_a_stale_fact_is_hidden_from_readers_like_after_the_sweep(world):
    await world.seed_stale("old plan")
    await world.seed("still fine")
    assert {f.text for f in await world.repo.get_active_facts(ACC)} == {"still fine"}


async def test_the_counters_and_the_sink_are_wired_on_the_same_world(world):
    await world.counters.add(ACC, NOW.date(), {"expiry_tool_errors": 2})
    [row] = await world.counters.for_day(NOW.date())
    assert (row.account_id, row.expiry_tool_errors) == (ACC, 2)
    await world.sink.post("alert")
    assert world.sink.texts == ["alert"]

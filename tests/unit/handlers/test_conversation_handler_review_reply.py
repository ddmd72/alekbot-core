"""ConversationHandler: the owner's reply to a memory-review question (Facts S4 · T8f, plan §3.16, §3.9).

The real `FactAnswerService` on the review world (the real review store, repository, typed write path, session store,
counters and task queue over one evaluating Firestore fake), a scripted coordinator that records every model call, and a
channel that records what was posted. Assertions: what was posted and where, what is stored (the question, the fact, the
history), the queued tasks, and that no model call happened. The handler decides nothing about questions: the cases that
are not a reply to a question show the ordinary flow answering."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.channel_binding import ChannelBinding
from src.domain.entities import ExpiryKind
from src.domain.fact_review_frame import reply_part
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from src.domain.messaging import FileAttachment, MessageContext, MessageRef, SmartResponse
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.ports.fact_update_port import FactUpdatePort
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld

USER = "u1"
TZ = "Europe/Lisbon"
CHANNEL = "D1"
SESSION = f"{USER}:{CHANNEL}"
TEXT = "User trains at the Iron Temple gym."
OTHER_TEXT = "User borrows books from the Example Library."
THREAD = "1729760000.000100"          # the question message's ts: a reply's thread_ts


class FixedZones:
    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(USER, TZ)


class ScriptedWrites(FactUpdatePort):
    """A typed write path that answers `outcome(fact_id)` and writes nothing."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        return self.outcome(fact_id)


class Rig:
    def __init__(self, world: ReviewWorld, with_answers: bool = True, binding: Optional[ChannelBinding] = None) -> None:
        self.world = world
        self.texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                       LanguageCode.EN, clock=world.clock)
        self.answers = FactAnswerService(
            fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
            task_queue=world.queue, session_store=world.session_store, texts=self.texts, interactive=world.interactive,
            owner_zones=FixedZones(), clock=world.clock,
        )
        self.coordinator = MagicMock()
        smart = AgentResponse.success(task_id="t1", agent_id=f"smart_response_agent_{USER}",
                                      result=SmartResponse(text="Sure."))
        self.coordinator.route_message = AsyncMock(return_value=smart)
        self.coordinator.handle_delegation = AsyncMock(return_value=smart)
        agent_factory = MagicMock()
        agent_factory.ensure_agents_for_user = AsyncMock()
        agent_factory.get_session_store = MagicMock(return_value=world.session_store)
        agent_factory.invalidate_prompt_cache = MagicMock()
        agent_factory.user_repo = MagicMock()
        agent_factory.user_repo.get_user = AsyncMock(return_value=None)
        bindings = None
        if binding is not None:
            bindings = MagicMock()
            bindings.get = AsyncMock(return_value=binding)
        self.handler = ConversationHandler(
            coordinator=self.coordinator, agent_factory=agent_factory, file_service=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30), channel_binding_service=bindings,
            fact_answers=self.answers if with_answers else None,
        )
        self.channel = channel()

    @property
    def model_calls(self) -> int:
        return self.coordinator.route_message.await_count + self.coordinator.handle_delegation.await_count

    @property
    def posted(self) -> List[str]:
        """Everything the channel was asked to say, acknowledgements (replies) and ordinary messages alike."""
        return [call.args[0] for call in (*self.channel.send_reply.await_args_list,
                                          *self.channel.send_message.await_args_list)]

    async def send(self, context: MessageContext) -> None:
        await self.handler.handle_message(context, self.channel)


def channel(platform: str = "slack", channel_id: str = CHANNEL) -> MagicMock:
    ch = MagicMock()
    ch.platform = platform
    ch.channel_id = channel_id
    ch.send_status_with_phrase = AsyncMock(return_value=("status-1", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_reply = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def reply_context(text: str, replied_to: Optional[str] = "m1", thread_id: Optional[str] = THREAD,
                  slack_channel: Optional[str] = CHANNEL, **fields: Any) -> MessageContext:
    """A Slack thread reply (`channel` in the metadata) unless `slack_channel` is None (a mention: the channel is the
    response channel's only)."""
    metadata: dict = {"event_type": "message", "slack_user_id": "S1", "turn_id": None}
    if slack_channel is not None:
        metadata["channel"] = slack_channel
    if replied_to is not None:
        metadata["reply_to_message_id"] = replied_to
    return MessageContext(text=text, session_id=SESSION, user_id=USER, account_id=ACC, thread_id=thread_id,
                          metadata=metadata, **fields)


async def stale_fact(world: ReviewWorld, text: str = TEXT) -> str:
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))


async def asked_question(world: ReviewWorld, fact_id: str, question_id: str = "q1", platform: str = "slack",
                         channel_id: str = CHANNEL, status: QuestionStatus = QuestionStatus.ASKED) -> FactReviewQuestion:
    """A question about `fact_id`, posted on Slack through the world's port (ref `slack`/`D1`/`m1`) or, for Telegram,
    at the ref Telegram gives."""
    base = dict(question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
                quoted_text=world.docs[fact_id]["text"])
    draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive", **base)
    labels = await FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                     LanguageCode.EN, clock=world.clock).labels_for(USER, ACC)
    if platform == "slack":
        ref = await world.interactive.post("slack", channel_id, fact_question_message(draft, labels))
    else:
        ref = MessageRef(platform=platform, channel_id=channel_id, message_id="55")
    return await world.seed_question(status, message_ref=ref, session_id=SESSION, **base)


async def history(world: ReviewWorld) -> List[tuple]:
    return [(m.role, m.parts[0].text) for m in await world.session_store.messages(SESSION)]


def stored(world: ReviewWorld, question_id: str = "q1") -> dict:
    return world.question_docs[question_id]


def ack(world: ReviewWorld, message: UIMessage) -> str:
    return world.localization.get_ui_string(LanguageCode.EN, message)


def next_tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review" and body.get("mode") == "next"]


async def day_row(world: ReviewWorld):
    """The account's counters of today: the handler hands the service the moment the reply arrived (the clock's now)."""
    rows = [row for row in await world.counters.for_day(datetime.now(timezone.utc).date()) if row.account_id == ACC]
    return rows[0] if rows else None


async def world_with_question(**kw: Any) -> tuple:
    world = ReviewWorld()
    await world.seed_state(account_id=ACC, current_question_id="q1")
    fact = await stale_fact(world)
    await asked_question(world, fact, **kw)
    await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER, fact_id="f-q2",
                              quoted_text=OTHER_TEXT)
    return world, fact


# ------------------------------------------------------------------------------------------------ taken at intake

class TestReplyTaken:
    async def test_a_thread_reply_with_a_digit_is_applied_noted_in_the_thread_and_never_reaches_the_model(self):
        world, fact = await world_with_question()
        before = set(world.docs)
        rig = Rig(world)

        await rig.send(reply_context("2"))

        assert rig.model_calls == 0
        assert rig.posted == [ack(world, UIMessage.MEMORY_ANSWER_NOTED)]
        assert rig.channel.send_reply.await_args.args == (ack(world, UIMessage.MEMORY_ANSWER_NOTED), "m1")
        rig.channel.send_message.assert_not_awaited()
        rig.channel.send_status_with_phrase.assert_not_awaited()           # no status for a turn that is not one
        assert world.docs[fact]["state"] == "archived"
        [history_fact] = set(world.docs) - before
        assert world.docs[history_fact]["text"].startswith("No longer true (the owner said so in Oct 2026): ")
        done = stored(world)
        assert (done["status"], done["closed_by"], done["answer"], done["answer_source"]) == (
            "closed", "owner", "ended", "digit")
        assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]

    async def test_a_thread_reply_in_words_is_recorded_noted_applies_nothing_and_releases_the_next(self):
        world, fact = await world_with_question()
        docs = {k: dict(v) for k, v in world.docs.items()}
        rig = Rig(world)

        await rig.send(reply_context("yes, still the case"))

        assert rig.model_calls == 0
        assert rig.posted == [ack(world, UIMessage.MEMORY_REPLY_NOTED)]
        waiting = stored(world)
        assert waiting["status"] == "asked" and waiting["reply_text"] == "yes, still the case"
        assert world.docs == docs                                  # the fact is the review's to decide
        assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
        assert (await world.review_store.get_state(ACC)).current_question_id is None     # the words gave up the slot
        assert stored(world, "q2")["status"] == "queued"           # released for a later run, not sent here

    async def test_a_reply_to_a_closed_question_is_already_answered_and_never_reaches_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        await rig.send(reply_context("1"))
        rig.channel.send_reply.reset_mock()

        await rig.send(reply_context("3"))

        assert rig.model_calls == 0
        assert rig.posted == [ack(world, UIMessage.MEMORY_ALREADY_ANSWERED)]
        assert world.docs[fact]["state"] == "current" and stored(world)["answer"] == "still_true"

    async def test_the_stored_pair_carries_the_marker_and_the_owners_text_verbatim(self):
        world, _ = await world_with_question()
        rig = Rig(world)

        await rig.send(reply_context("  a gym, not a pool\nthe Iron Temple one  "))

        assert await history(world) == [
            ("user", reply_part("q1", "  a gym, not a pool\nthe Iron Temple one  ")),
            ("model", ack(world, UIMessage.MEMORY_REPLY_NOTED)),
        ]
        assert world.session_store.overflow == []

    async def test_a_backtick_wrapped_digit_is_a_digit_and_stored_as_typed(self):
        world, _ = await world_with_question()
        rig = Rig(world)

        await rig.send(reply_context("`4`"))

        assert stored(world)["answer"] == "dont_know"
        assert (await history(world))[0] == ("user", "[Reply to memory review q=q1] `4`")

    async def test_a_telegram_reply_is_matched_by_the_chat_and_the_message_id(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await asked_question(world, fact, platform="telegram", channel_id="777")
        rig = Rig(world)
        rig.channel = channel("telegram", "777")
        context = MessageContext(
            text="3", session_id=SESSION, user_id=USER, account_id=ACC, thread_id=None,
            metadata={"platform": "telegram", "chat_id": 777, "reply_to_message_id": "55"})

        await rig.send(context)

        assert rig.model_calls == 0 and rig.posted == [ack(world, UIMessage.MEMORY_ANSWER_NOTED)]
        assert rig.channel.send_reply.await_args.args == (ack(world, UIMessage.MEMORY_ANSWER_NOTED), "55")
        assert world.docs[fact]["state"] == "invalidated"

    async def test_a_mention_reply_takes_its_channel_from_the_response_channel(self):
        world, fact = await world_with_question()
        rig = Rig(world)

        await rig.send(reply_context("1", slack_channel=None))

        assert rig.model_calls == 0 and stored(world)["status"] == "closed"
        assert rig.channel.send_reply.await_args.args[1] == "m1"          # the question's ts, not the reply's own

    async def test_a_reply_to_a_posted_question_not_marked_asked_is_taken(self):
        world, fact = await world_with_question(status=QuestionStatus.PENDING)
        appended = stored(world)["appended_at"]
        rig = Rig(world)

        await rig.send(reply_context("2"))

        assert rig.model_calls == 0 and rig.posted == [ack(world, UIMessage.MEMORY_ANSWER_NOTED)]
        assert stored(world)["asked_at"] == appended and world.docs[fact]["state"] == "archived"

    async def test_an_acknowledgement_that_cannot_be_posted_is_logged_and_the_message_is_not_answered_twice(self, caplog):
        world, _ = await world_with_question()
        rig = Rig(world)
        rig.channel.send_reply.side_effect = RuntimeError("slack down")

        with caplog.at_level(logging.ERROR):
            await rig.send(reply_context("1"))

        assert rig.model_calls == 0 and stored(world)["status"] == "closed"
        assert any("acknowledgement not posted" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------------ left to the model

class TestOrdinaryConversation:
    async def test_a_top_level_digit_goes_to_the_model_and_the_question_stays_open(self):
        world, fact = await world_with_question()
        rig = Rig(world)

        await rig.send(reply_context("2", replied_to=None, thread_id=None))

        assert rig.model_calls == 1
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"
        assert ack(world, UIMessage.MEMORY_ANSWER_NOTED) not in rig.posted

    async def test_a_reply_to_another_bot_message_goes_to_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world)

        await rig.send(reply_context("2", replied_to="1729760999.000900"))

        assert rig.model_calls == 1 and stored(world)["status"] == "asked"

    async def test_the_same_message_id_in_another_channel_goes_to_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        rig.channel = channel("slack", "D2")

        await rig.send(reply_context("2", slack_channel="D2"))

        assert rig.model_calls == 1 and stored(world)["status"] == "asked"

    async def test_a_reply_with_an_attachment_goes_to_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        attachment = FileAttachment(url="https://files.example.com/receipt.pdf", mime_type="application/pdf",
                                    filename="receipt.pdf")

        await rig.send(reply_context("2", attachments=[attachment]))

        assert rig.model_calls == 1 and stored(world)["status"] == "asked"

    async def test_a_handler_built_without_the_answer_service_sends_every_reply_to_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world, with_answers=False)

        await rig.send(reply_context("2"))

        assert rig.model_calls == 1 and stored(world)["status"] == "asked"

    async def test_a_store_error_lets_the_model_answer_and_counts_the_failure(self, caplog):
        world, fact = await world_with_question()
        rig = Rig(world)

        async def broken(*args: Any) -> Any:
            raise RuntimeError("store unavailable")

        world.review_store.by_message = broken  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR):
            await rig.send(reply_context("2"))

        assert rig.model_calls == 1
        assert (await day_row(world)).reply_intake_failed == 1
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"
        assert any("store unavailable" in r.getMessage() for r in caplog.records)

    async def test_a_digit_the_write_rules_refused_goes_to_the_model_with_the_question_open(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        rig.answers._fact_updates = ScriptedWrites(
            lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.INVALID, "refused by the rules"))

        await rig.send(reply_context("1"))

        assert rig.model_calls == 1 and stored(world)["status"] == "asked"


class TestFactChanged:
    async def test_a_digit_on_a_fact_that_changed_is_told_so_in_the_reply_without_the_model(self):
        world, fact = await world_with_question()
        world.docs[fact]["state"] = "archived"
        rig = Rig(world)

        await rig.send(reply_context("1"))

        text = ack(world, UIMessage.MEMORY_FACT_CHANGED)
        assert rig.model_calls == 0 and rig.posted == [text]
        assert rig.channel.send_reply.await_args.args == (text, "m1")
        assert stored(world)["status"] == "asked" and next_tasks(world) == []
        assert await history(world) == [("user", reply_part("q1", "1")), ("model", text)]
        row = await day_row(world)
        assert row is None or row.reply_intake_failed == 0

    async def test_a_digit_whose_write_conflicted_is_told_so_in_the_reply_without_the_model(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        rig.answers._fact_updates = ScriptedWrites(
            lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.CONFLICT, "changed meanwhile"))

        await rig.send(reply_context("2"))

        text = ack(world, UIMessage.MEMORY_FACT_CHANGED)
        assert rig.model_calls == 0 and rig.posted == [text]
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"
        assert await history(world) == [("user", reply_part("q1", "2")), ("model", text)]


# ------------------------------------------------------------------------------------------------ not an interactive turn

class TestOtherTurns:
    async def test_a_bound_channel_turn_never_takes_the_branch(self):
        world, fact = await world_with_question()
        binding = ChannelBinding(channel_id=CHANNEL, agent_type="domain_researcher", intent="domain_research",
                                 created_by=USER, companion_config=None)
        rig = Rig(world, binding=binding)

        await rig.send(reply_context("2"))

        assert rig.coordinator.handle_delegation.await_count == 1 and rig.coordinator.route_message.await_count == 0
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"

    async def test_a_command_in_the_thread_is_a_command_not_a_reply(self):
        world, fact = await world_with_question()
        rig = Rig(world)
        context = reply_context("memory save ab12")
        context.metadata["event_type"] = "command"

        await rig.handler.handle_command("memory save ab12", context, rig.channel)

        assert stored(world)["status"] == "asked" and rig.model_calls == 0
        assert ack(world, UIMessage.MEMORY_REPLY_NOTED) not in rig.posted
        assert stored(world)["reply_text"] is None

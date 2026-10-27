"""P13 card D: what a chat turn hands to the coordinator decides where its async results land.

From `ConversationHandler.handle_message` through the real coordinator (real `AgentRegistry`, raw `GcpTaskQueue`) to the
created `agent_execution` task, and from that task, run through `/worker`, to the chat (`SeamWorld`,
tests/support/seam_world.py). The agents are scripted stand-ins: the Router stand-in collapses Router and Smart, which
both spread the turn's context into the delegation (`router_agent.py:380-388`, `smart_response_agent.py:573`); it
delegates exactly as Smart's engine does (contract 4). The chat channel is a recording `ChatChannel` (the platform
boundary). The primary is `("slack", "C1")` unless a test says otherwise; the turn is in another channel.

Pins P13-D1: a Telegram turn carries `origin_channel_id` None, so its async results go to the primary channel.
"""
import asyncio
from datetime import timedelta
from typing import Any, List, Optional, Tuple

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.consolidation_run import ConsolidationRun
from src.domain.llm import Message, MessagePart
from src.domain.messaging import MessageContext
from src.domain.ui_messages import UIMessage
from src.locales.uk import UI_STRINGS
from tests.support.seam_world import (
    ACCOUNT, OTHER_SLACK, PRIMARY, SEAM_CONSOLIDATION, T0, TELEGRAM, USER, Delegation, SeamWorld, Step, page, require,
    says,
)

ON_ITS_WAY = "Your page about heat pumps in Springfield is on its way."
PAGE_FILE = "heat-pumps.html"
PAGE_LABEL = "Heat pumps in Springfield.html"


class ChatChannel:
    """The platform response channel of one chat turn (no `MagicMock`): the methods `ConversationHandler` calls on a turn
    and on a command (`test_conversation_handler_interactive_delivery.py:31-44`), recording what was sent."""

    def __init__(self, platform: str, channel_id: str) -> None:
        self.platform = platform
        self.channel_id = channel_id
        self.max_message_length = 4000
        self.supports_message_editing = True
        self.texts: List[str] = []
        self.statuses: List[Any] = []
        self.rich: List[Any] = []

    async def send_status_with_phrase(self, status_type: Any, thread_id: Optional[str] = None) -> Tuple[str, str]:
        self.statuses.append(status_type)
        return "m1", "thinking"

    async def get_status_phrase(self, status_type: Any) -> str:
        return "processing"

    async def update_status_with_phrase_and_dots(self, message_id: str, phrase: str, dots: int) -> None:
        return None

    async def send_status(self, status: Any, thread_id: Optional[str] = None) -> None:
        self.statuses.append(status)

    async def send_message(self, text: str, thread_id: Optional[str] = None) -> None:
        self.texts.append(text)

    async def send_chunked_message(self, text: str, status_message_id: Optional[str] = None,
                                   thread_id: Optional[str] = None, link_list: Optional[list] = None) -> None:
        self.texts.append(text)

    async def send_flat_response(self, text: str, status_message_id: Optional[str] = None) -> None:
        self.texts.append(text)

    async def update_message(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def send_rich_content(self, content: Any, thread_id: Optional[str] = None) -> None:
        self.rich.append(content)


def _slack_turn(text: str) -> MessageContext:
    """As `src/adapters/slack/http_adapter.py:512-530` builds it, without `turn_id` (no long-turn service here)."""
    return MessageContext(text=text, session_id="u1:C2", user_id=USER, account_id=ACCOUNT,
                          metadata={"channel": "C2", "event_type": "message"})


def _telegram_turn(text: str) -> MessageContext:
    """As `src/adapters/telegram/webhook_adapter.py:283-301` builds it: `platform` and `chat_id`, no `channel` key, no
    `turn_id`."""
    return MessageContext(text=text, session_id="u1:7001", user_id=USER, account_id=ACCOUNT,
                          metadata={"platform": "telegram", "chat_id": 7001, "telegram_user_id": "9001"})


def _asks_for_a_page(world: SeamWorld) -> None:
    world.agent("router_agent").play(
        Step(says(ON_ITS_WAY), Delegation("create_html_page", "A page about heat pumps in Springfield")))
    world.agent("html_page_generator_agent").play(page(PAGE_FILE, PAGE_LABEL))


async def test_a_page_asked_for_in_a_slack_channel_is_delivered_there(monkeypatch):
    """Control. Guards `conversation_handler.py:651` (`channel` → `origin_channel_id` at `:936`) and `:937`
    (`origin_platform`), `:930` (`session_id`) → `agent_coordinator.py:429,562-563` (spread into the task's context)
    → `gcp:207-213,224-225` → `agent_worker_handler.py:258-259,281` → `ums.py:119-125` (the override) and `:550`
    (the session). Dropping either origin keyword sends the page to the primary C1. (The coordinator's
    `deadline_seconds` forward is pinned by `deadline_s == 1220`, `agent_manifest.py:524`.)"""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    _asks_for_a_page(world)
    chat = ChatChannel(*OTHER_SLACK)

    await world.chat_handler().handle_message(_slack_turn("Make me a page about heat pumps in Springfield."), chat)

    require(not world.unscripted(), "every agent call was scripted")
    require(chat.texts == [ON_ITS_WAY], "the turn answered in its own chat")
    [request] = world.requests("agent_execution")
    assert request.deadline_s == 1220
    context = request.body["context"]
    assert context["session_id"] == "u1:C2"
    assert context["origin_channel_id"] == "C2"
    assert context["origin_platform"] == "slack"
    assert context["interactive_delivery"] is True

    answer = await world.run(request.body)

    require(not world.unscripted(), "every agent call was scripted")
    require(answer[1] == 200, "the delivery was answered 200")
    [(label, url)] = world.docs_to(*OTHER_SLACK)
    assert label == PAGE_LABEL and url.startswith("https://svc.test.example/f/")
    assert world.channels.posts == world.posts_to(*OTHER_SLACK)
    assert world.posts_to(*PRIMARY) == []
    [key] = world.stored_keys()
    history = world.history("u1:C2")
    assert ("user", f"[System: async document ready — {PAGE_LABEL}]", None) in history
    assert any(role == "model" and f'"file_ref": "{key}"' in (text or "") for role, text, _ in history)
    assert world.history("u1:C1") == []


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="P13-D1: a Telegram chat turn carries origin_channel_id None, so its async results go to the primary "
           "channel; fix after the plan (owner)",
)
async def test_a_page_asked_for_in_a_telegram_chat_is_delivered_there(monkeypatch):
    """P13-D1. A Telegram message's metadata has no `channel` key (`webhook_adapter.py:283-301`), so
    `conversation_handler.py:651` reads None and `:936` forwards `origin_channel_id` None; `ums.py:119` then needs both
    overrides and falls back to the primary (`:129-131`). Guards `conversation_handler.py:936,937` →
    `agent_coordinator.py:562-563` → `agent_worker_handler.py:258-259,281` → `ums.py:119-125`.

    Flipping fix: `conversation_handler.py:936` → `"origin_channel_id": channel_id or getattr(response_channel,
    "channel_id", None)` (the idiom of `:696` and `:1121`; not at `:651`, which would open channel bindings to Telegram
    against the owner's P9-D9 ruling). The preconditions hold today and after that fix."""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    _asks_for_a_page(world)
    chat = ChatChannel(*TELEGRAM)

    await world.chat_handler().handle_message(_telegram_turn("Make me a page about heat pumps in Springfield."), chat)

    require(not world.unscripted(), "every agent call was scripted")
    requests = world.requests("agent_execution")
    require(len(requests) == 1, "exactly one agent_execution task was created")
    context = requests[0].body["context"]
    require(context.get("origin_platform") == "telegram", "the task carries the Telegram origin platform")
    require(context.get("session_id") == "u1:7001", "the task carries the Telegram session")

    answer = await world.run(requests[0].body)

    require(not world.unscripted(), "every agent call was scripted")
    require(answer[1] == 200, "the delivery was answered 200")
    posts = world.channels.posts
    require(len(posts) == 1 and posts[0].kind == "doc", "exactly one document post in the world")
    assert (posts[0].platform, posts[0].channel_id) == TELEGRAM


async def test_a_telegram_chat_becomes_the_last_active_channel_for_a_reminder(monkeypatch):
    """No primary. Guards `conversation_handler.py:696-701` (the platform and the channel, with the response channel's
    id as the fallback the origin lacks) → `firestore_notification_state_adapter.py:30` → `ums.py:137` (the
    last-active fallback a reminder resolves through). Passes today; with the P13-D1 pin it documents the asymmetry
    between the saved channel and the forwarded origin."""
    world = SeamWorld(monkeypatch)
    world.agent("router_agent").play(says("Noted."))
    chat = ChatChannel(*TELEGRAM)

    await world.chat_handler().handle_message(_telegram_turn("Hello from Telegram."), chat)

    stored = None
    for _ in range(10):  # drain the fire-and-forget save (conversation_handler.py:697-705)
        stored = await world.state.get(USER)
        if stored is not None:
            break
        await asyncio.sleep(0)
    require(stored is not None, "the turn saved a last-active channel")
    require(chat.texts == ["Noted."], "the turn answered in its own chat")

    note_id = await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(hours=1))
    world.agent("smart_response_agent").play(says("Time to call dad."))

    answer = await world.run({"task_type": "execute_reminder", "note_id": note_id, "user_id": USER,
                              "due_at": T0.isoformat()})

    require(not world.unscripted(), "every agent call was scripted")
    require(answer == ({"status": "ok"}, 200), "the reminder fired")
    assert [(p.kind, p.value) for p in world.posts_to(*TELEGRAM)] == [("text", "Time to call dad.")]
    assert world.channels.posts == world.posts_to(*TELEGRAM)


async def test_consolidate_answers_in_the_chat_it_was_typed_in(monkeypatch):
    """Guards `conversation_handler.py:1647` (`channel`) and `:1648` (`platform`) of the command's metadata →
    `ums.py:119-125`. Dropping either sends the outcome to the primary C1. The `$consolidate` setup follows
    `test_consolidate_command_interleaving.py:38-98` (re-declared here) over the real `FirestoreConsolidationQueue`."""
    world = SeamWorld(monkeypatch)
    await world.set_primary(*PRIMARY)
    batch_size = SEAM_CONSOLIDATION.batch_size  # the chat handler's settings; u1 has no override
    history = [Message(role="user" if i % 2 == 0 else "model", parts=[MessagePart(text=f"old message {i}")])
               for i in range(batch_size + 2)]
    await world.sessions.append_messages_batch("u1:7001", history, owner_id=USER, raise_on_error=True)
    quick = world.agent("quick_response_agent").play(says("Memory consolidation finished."))

    async def consolidate_now(user_id: str) -> ConsolidationRun:
        return ConsolidationRun(processed=1)

    handler = world.chat_handler(consolidation_queue=FirestoreConsolidationQueue(world.db, world.env),
                                 overflow_callback=consolidate_now)
    chat = ChatChannel(*TELEGRAM)
    context = MessageContext(text="$consolidate", session_id="u1:7001", user_id=USER, account_id=ACCOUNT,
                             metadata={"event_type": "command", "platform": "telegram", "channel": "7001",
                                       "chat_id": 7001, "telegram_user_id": "9001"})

    await handler.handle_command("consolidate", context, chat)

    require(not world.unscripted(), "every agent call was scripted")
    require(chat.texts == [UI_STRINGS[UIMessage.CONSOLIDATION_STARTED.value]], "the command was acknowledged")
    queued = world.db.collection(world.env.consolidation_queue_collection).docs
    require(len(queued) == 1, "one batch was queued")
    require(len(quick.received) == 1, "the outcome was formatted once")
    assert [(p.kind, p.value) for p in world.posts_to(*TELEGRAM)] == [("text", "Memory consolidation finished.")]
    assert world.posts_to(*PRIMARY) == []

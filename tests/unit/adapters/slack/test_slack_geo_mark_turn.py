"""`HTTPModeAdapter.handle_geo_mark_turn` (GEO_MARKS_RFC §4.5): a queued `$geo <text>` runs one chat turn with the mark
in front of the owner's words, for the right user, channel and session, under the session lock; and the message path
that shares its turn helper still builds the context it always did."""
import asyncio
import weakref
from types import SimpleNamespace
from typing import List, Optional, Tuple

from src.adapters.slack import http_adapter
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode

MARK = "[Shared location: 12 Oak Street, Springfield (±40 m)]"
SESSION = "usr-1:100"
TURN = {"platform": "slack", "user_id": "usr-1", "account_id": "acc-1", "channel_id": "D100", "session_id": SESSION,
        "platform_user_id": "U1", "text": "Is the Iron Temple gym open?", "mark_text": MARK, "language": "en",
        "link_fp": "18e09df9"}


class Handler:
    def __init__(self) -> None:
        self.messages: List[Tuple[object, object]] = []
        self.commands: List[Tuple[str, object]] = []
        self.fail = False

    async def handle_message(self, context, channel) -> None:
        if self.fail:
            raise RuntimeError("model down")
        self.messages.append((context, channel))

    async def handle_command(self, command, context, channel) -> None:
        self.commands.append((command, context))


class Iam:
    def __init__(self, action: str = "allow", user_id: Optional[str] = "usr-1") -> None:
        self.action, self.user_id = action, user_id
        self.asked: List[str] = []

    async def authorize(self, platform: str, platform_user_id: str):
        self.asked.append(platform_user_id)
        user = None if self.user_id is None else SimpleNamespace(user_id=self.user_id, account_id="acc-1",
                                                                 display_name="Alex Doe")
        return SimpleNamespace(action=self.action, user=user, message=None)


def _adapter(iam: Optional[Iam] = None) -> Tuple[HTTPModeAdapter, Handler]:
    handler = Handler()
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = handler
    adapter.iam_service = iam or Iam()
    adapter.app = SimpleNamespace(client=object())
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._session_locks = weakref.WeakValueDictionary()

    async def resolve(user_id: str):
        return LanguageCode.EN

    adapter._resolve_language = resolve  # type: ignore[method-assign]
    return adapter, handler


async def test_the_payload_runs_one_turn_with_the_mark_and_the_owners_words():
    adapter, handler = _adapter()
    body, status = await adapter.handle_geo_mark_turn(TURN, {})
    assert (body, status) == ({"ok": True}, 200)
    [(context, channel)] = handler.messages
    assert context.text == TURN["text"]
    assert context.geo_mark_note == MARK
    assert (context.user_id, context.account_id, context.session_id) == ("usr-1", "acc-1", SESSION)
    assert context.metadata["channel"] == "D100" and context.metadata["slack_user_id"] == "U1"
    assert context.metadata["turn_id"] == "geo:18e09df9"
    assert channel.channel_id == "D100"
    assert adapter.iam_service.asked == ["U1"]


async def test_a_question_that_starts_with_a_dollar_is_a_question_not_a_command():
    adapter, handler = _adapter()
    await adapter.handle_geo_mark_turn({**TURN, "text": "$memory what is it"}, {})
    assert handler.commands == []
    assert handler.messages[0][0].text == "$memory what is it"


async def test_a_rejected_user_runs_no_turn():
    adapter, handler = _adapter(Iam(action="reject"))
    body, status = await adapter.handle_geo_mark_turn(TURN, {})
    assert (body, status) == ({"ok": True, "dropped": "unauthorized"}, 200)
    assert handler.messages == []


async def test_an_allowed_decision_without_a_user_runs_no_turn():
    adapter, handler = _adapter(Iam(user_id=None))
    assert (await adapter.handle_geo_mark_turn(TURN, {}))[0]["dropped"] == "unauthorized"
    assert handler.messages == []


async def test_a_slack_user_who_is_another_account_than_the_links_owner_runs_no_turn():
    adapter, handler = _adapter(Iam(user_id="usr-2"))
    assert (await adapter.handle_geo_mark_turn(TURN, {}))[0]["dropped"] == "user_mismatch"
    assert handler.messages == []


async def test_a_turn_that_fails_answers_500_and_frees_the_lock():
    adapter, handler = _adapter()
    handler.fail = True
    # the turn helper's errors are the handler's to raise; the entry reports them to the queue for a retry
    body, status = await adapter.handle_geo_mark_turn(TURN, {})
    assert status == 500 and "model down" in body["error"]
    handler.fail = False
    assert (await adapter.handle_geo_mark_turn(TURN, {}))[1] == 200


async def test_a_busy_session_waits_on_the_same_lock_then_runs():
    adapter, handler = _adapter()
    lock = asyncio.Lock()
    adapter._session_locks[SESSION] = lock
    await lock.acquire()
    turn = asyncio.create_task(adapter.handle_geo_mark_turn(TURN, {}))
    await asyncio.sleep(0.05)
    assert handler.messages == [] and not turn.done()
    lock.release()
    assert (await turn)[1] == 200
    assert len(handler.messages) == 1
    assert not lock.locked()


async def test_a_session_busy_past_the_wait_is_left_to_the_queue_retry(monkeypatch):
    monkeypatch.setattr(http_adapter, "BUSY_WAIT_S", 0.01)
    adapter, handler = _adapter()
    lock = asyncio.Lock()
    adapter._session_locks[SESSION] = lock
    await lock.acquire()
    body, status = await adapter.handle_geo_mark_turn(TURN, {})
    assert status == 503 and handler.messages == []
    assert lock.locked()


async def test_the_long_turn_hook_releases_the_session_lock():
    adapter, handler = _adapter()

    async def long_turn(context, channel):
        await channel.on_long_turn()
        assert not adapter._session_locks[SESSION].locked()

    handler.handle_message = long_turn  # type: ignore[method-assign]
    assert (await adapter.handle_geo_mark_turn(TURN, {}))[1] == 200


async def test_a_message_event_still_builds_its_context_through_the_shared_helper():
    adapter, handler = _adapter()
    event = {"type": "message", "text": "hi", "channel": "D100", "user": "U1", "ts": "100.5",
             "channel_type": "mpim", "thread_ts": "90.1"}
    await adapter._process_message_event(event, "ignored", turn_id="slack:Ev1")
    [(context, _)] = handler.messages
    assert context.text == "hi" and context.geo_mark_note is None
    assert context.session_id == "usr-1:D100"
    assert context.thread_id == "90.1"
    assert context.metadata["channel_type"] == "mpim"
    assert context.metadata["origin_message_id"] == "100.5" and context.metadata["event_time"] == 100.5
    assert context.metadata["turn_id"] == "slack:Ev1"


async def test_a_message_event_that_starts_with_a_dollar_is_still_a_command():
    adapter, handler = _adapter()
    await adapter._process_message_event({"type": "message", "text": "$geo where", "channel": "D100", "user": "U1",
                                          "ts": "1.1"}, "x")
    assert handler.messages == [] and handler.commands[0][0] == "geo where"


async def test_an_authorized_decision_without_a_user_is_skipped_on_the_message_path():
    adapter, handler = _adapter(Iam(user_id=None))
    await adapter._process_message_event({"type": "message", "text": "hi", "channel": "D100", "user": "U1",
                                          "ts": "1.1"}, "x")
    assert handler.messages == []

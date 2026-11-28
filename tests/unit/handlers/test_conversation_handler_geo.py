"""ConversationHandler and geo marks (GEO_MARKS_RFC §4.5, §4.6): `$geo` issues the one-time link, `handle_geo_mark`
records a platform-native share, and a queued turn's `geo_mark_note` is the user-message part before the owner's text.

The service is the real `GeoMarkService` over the real link and session stores on the evaluating Firestore fake."""
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from tests.support.active_users import ActiveUsers
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.domain.agent import AgentResponse
from src.domain.geo_mark import GeoFix, GeoPlace, GeoSource
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.locales import en
from src.services.geo_mark_service import GeoMarkService
from src.services.localization_service import LocalizationService
from tests.support.fact_review_world import ReviewSessionStore
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

SESSION = "usr-1:C1"
QUESTION = "Is the Iron Temple gym open?"
ADDRESS = "Main Street, Springfield"
AT_FIX = GeoFix.parse(41.1, -8.6, 5, datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc), GeoSource.TELEGRAM_PIN)


def _ui(message: UIMessage, **fmt) -> str:
    template = en.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


class Geocoder:
    async def look_up(self, latitude, longitude):
        return GeoPlace(ADDRESS, (), ())


class Channel:
    def __init__(self, platform: str = "slack") -> None:
        self.platform = platform
        self.texts: List[str] = []
        self.links: List[Tuple[str, str]] = []

    async def send_message(self, text, thread_id=None, link_list=None):
        self.texts.append(text)

    async def send_document_link(self, url, label, thread_id=None):
        self.links.append((url, label))


class Bindings:
    def __init__(self, bound: Optional[set] = None) -> None:
        self.bound = bound or set()

    async def get(self, channel_id):
        return SimpleNamespace(agent_type="drive") if channel_id in self.bound else None


class World:
    def __init__(self, with_service: bool = True, bound: Optional[set] = None) -> None:
        self.db = EvalDb()
        self.links = FirestoreEphemeralStore(self.db, "t_geo_mark_links")
        self.sessions = ReviewSessionStore(self.db, "t_")
        queue, _ = make_task_queue()
        users = SimpleNamespace(get_user=AsyncMock(return_value=None))
        self.service = GeoMarkService(
            links=self.links, geocoder=Geocoder(), sessions=self.sessions, channels=MagicMock(), task_queue=queue,
            users=users, localization=FileLocalizationAdapter(), digest_key=b"g" * 32, base_url="https://app.example",
            inactive_users=ActiveUsers())
        self.handler = ConversationHandler(
            coordinator=MagicMock(), agent_factory=MagicMock(),
            global_config=ConsolidationSettings(threshold=50, batch_size=30),
            localization=LocalizationService(FileLocalizationAdapter()),
            channel_binding_service=Bindings(bound) if bound is not None else None,
            commands=CommandServices(geo_marks=self.service if with_service else None),
        )

    async def history(self):
        return [(m.role, [p.text for p in m.parts]) for m in await self.sessions.messages(SESSION)]


def _command_context(text: str, platform: str = "slack") -> MessageContext:
    return MessageContext(text=text, session_id=SESSION, user_id="usr-1", account_id="acc-1", language="en",
                          metadata={"event_type": "command", "platform": platform, "slack_user_id": "U1",
                                    "channel": "C1", "channel_type": "im"})


async def _geo(world: World, text: str, platform: str = "slack") -> Channel:
    channel = Channel(platform)
    command = text.strip("`").lstrip("$").strip().lower()
    await world.handler.handle_command(command, _command_context(text, platform), channel)
    return channel


class TestGeoCommand:

    async def test_the_link_line_and_the_labelled_link_are_posted(self):
        w = World()

        channel = await _geo(w, "$geo")

        assert channel.texts == [_ui(UIMessage.GEO_LINK_LINE, minutes=10)]
        [(url, label)] = channel.links
        assert label == _ui(UIMessage.GEO_LINK_LABEL)
        assert url.startswith("https://app.example/geo/")
        binding = await w.service.peek_link(url.rsplit("/", 1)[1])
        assert binding is not None
        assert (binding.user_id, binding.account_id, binding.platform, binding.channel_id, binding.session_id,
                binding.platform_user_id, binding.language, binding.text) == (
            "usr-1", "acc-1", "slack", "C1", SESSION, "U1", "en", "")

    async def test_the_owners_text_keeps_its_case(self):
        w = World()

        channel = await _geo(w, f"$geo {QUESTION}")

        binding = await w.service.peek_link(channel.links[0][0].rsplit("/", 1)[1])
        assert binding is not None and binding.text == QUESTION

    async def test_a_copied_command_in_backticks_and_upper_case_still_carries_the_text(self):
        w = World()

        channel = await _geo(w, f"`$GEO   {QUESTION}  `")

        binding = await w.service.peek_link(channel.links[0][0].rsplit("/", 1)[1])
        assert binding is not None and binding.text == QUESTION

    async def test_a_word_that_only_starts_with_geo_is_not_the_command(self):
        w = World()
        channel = await _geo(w, "$geography")
        assert channel.texts == [_ui(UIMessage.UNKNOWN_COMMAND, command="geography")] and channel.links == []

    async def test_without_the_service_it_is_unavailable(self):
        channel = await _geo(World(with_service=False), "$geo")
        assert channel.texts == [_ui(UIMessage.GEO_UNAVAILABLE)] and channel.links == []

    async def test_in_a_bound_channel_it_says_so_and_issues_nothing(self):
        w = World(bound={"C1"})
        channel = await _geo(w, f"$geo {QUESTION}")
        assert channel.texts == [_ui(UIMessage.GEO_BOUND_CHANNEL)] and channel.links == []
        assert w.db.collection("t_geo_mark_links").docs == {}

    async def test_an_unbound_channel_with_bindings_configured_issues_the_link(self):
        channel = await _geo(World(bound={"C-other"}), "$geo")
        assert len(channel.links) == 1

    async def test_on_telegram_it_points_to_the_pin(self):
        w = World()
        channel = await _geo(w, "$geo", platform="telegram")
        assert channel.texts == [_ui(UIMessage.GEO_USE_TELEGRAM_PIN)] and channel.links == []
        assert w.db.collection("t_geo_mark_links").docs == {}

    async def test_an_issue_failure_answers_unavailable(self, monkeypatch):
        w = World()

        async def refuse(key, value, ttl_s):
            raise RuntimeError("firestore down")

        monkeypatch.setattr(w.links, "set", refuse)

        channel = await _geo(w, "$geo")

        assert channel.texts == [_ui(UIMessage.GEO_UNAVAILABLE)] and channel.links == []


class TestGeoOnlyInDirectMessages:

    @pytest.mark.parametrize("channel_type", ["channel", "group", "mpim", None])
    async def test_a_shared_conversation_gets_a_refusal_and_no_link(self, channel_type):
        w = World()
        context = _command_context(f"$geo {QUESTION}")
        if channel_type is None:
            del context.metadata["channel_type"]
        else:
            context.metadata["channel_type"] = channel_type
        channel = Channel("slack")
        await w.handler.handle_command("geo", context, channel)
        assert channel.texts == [_ui(UIMessage.GEO_DM_ONLY)] and channel.links == []
        assert w.db.collection("t_geo_mark_links").docs == {}

    async def test_a_direct_message_gets_a_link(self):
        channel = await _geo(World(), "$geo")
        assert len(channel.links) == 1


class TestHandleGeoMark:

    def _context(self) -> MessageContext:
        return MessageContext(text="", session_id=SESSION, user_id="usr-1", account_id="acc-1", language="en",
                              metadata={"platform": "telegram", "channel": "C1"})

    async def test_the_mark_is_recorded_on_the_contexts_session_and_channel(self):
        w = World()
        channel = Channel("telegram")

        await w.handler.handle_geo_mark(AT_FIX, self._context(), channel)

        line = _ui(UIMessage.GEO_SAVED, address=ADDRESS, accuracy="±5 m", time="18:40")
        assert channel.texts == [line]
        [(_, [mark_text]), (_, [model])] = await w.history()
        assert mark_text.startswith("[Geo mark:") and "as a Telegram location" in mark_text and model == line

    async def test_a_failed_append_says_not_saved(self):
        w = World()
        w.sessions.fail = True
        channel = Channel("telegram")

        await w.handler.handle_geo_mark(AT_FIX, self._context(), channel)

        assert channel.texts[-1] == _ui(UIMessage.GEO_NOT_SAVED)

    async def test_without_the_service_it_is_unavailable(self):
        channel = Channel("telegram")
        await World(with_service=False).handler.handle_geo_mark(AT_FIX, self._context(), channel)
        assert channel.texts == [_ui(UIMessage.GEO_UNAVAILABLE)]

    async def test_in_a_bound_channel_nothing_is_recorded(self):
        w = World(bound={"C1"})
        channel = Channel("telegram")

        await w.handler.handle_geo_mark(AT_FIX, self._context(), channel)

        assert channel.texts == [_ui(UIMessage.GEO_BOUND_CHANNEL)]
        assert await w.history() == []


# --- the queued turn's mark part --------------------------------------------------------------------------------------

def _turn_handler():
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="task-1", agent_id="smart_response_agent_usr-1", result=SmartResponse(text="OK")))
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    session_store.save_session = AsyncMock()
    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)
    agent_factory.user_repo = SimpleNamespace(get_user=AsyncMock(return_value=None))
    handler = ConversationHandler(coordinator=coordinator, agent_factory=agent_factory,
                                  global_config=ConsolidationSettings(threshold=50, batch_size=30))
    return handler, coordinator, session_store


def _turn_channel() -> MagicMock:
    ch = MagicMock()
    ch.channel_id = "C1"
    ch.platform = "slack"
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-1", "thinking..."))
    ch.get_status_phrase = AsyncMock(return_value="thinking")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


class TestGeoMarkNote:

    async def test_the_note_is_the_part_before_the_owners_text_and_is_persisted(self):
        handler, coordinator, store = _turn_handler()
        note = "[Geo mark: the user shared their current position, on purpose, from the location link in Slack]"
        context = MessageContext(text=QUESTION, session_id=SESSION, user_id="usr-1", account_id="acc-1",
                                 language="en", metadata={"channel": "C1"}, geo_mark_note=note)

        await handler.handle_message(context, _turn_channel())

        seen = [p.text for p in coordinator.route_message.await_args.args[0].context["current_message_parts"]]
        assert seen[:2] == [note, QUESTION]
        messages = store.append_messages_batch.await_args.args[1]
        user = next(m for m in messages if m.role == "user")
        assert [p.text for p in user.parts][:2] == [note, QUESTION]
        assert context.text == QUESTION       # the Router keeps reading the owner's own words

    async def test_without_a_note_the_turn_is_unchanged(self):
        handler, coordinator, store = _turn_handler()
        context = MessageContext(text=QUESTION, session_id=SESSION, user_id="usr-1", account_id="acc-1",
                                 language="en", metadata={"channel": "C1"})

        await handler.handle_message(context, _turn_channel())

        seen = [p.text for p in coordinator.route_message.await_args.args[0].context["current_message_parts"]]
        assert seen == [QUESTION]

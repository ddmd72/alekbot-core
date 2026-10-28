"""The note chain (DRIVE_FILE_NOTES_RFC §4.6, §4.9; design §5 "Seam world / chain"; arbiter Δ10, Δ11).

A note written through the drive specialist reaches the owner on both owner paths, as the real classes see it:
  - a CHAT turn: Smart's `DelegationEngine` → the real coordinator → the real lazy factory path builds the `DriveAgent`
    on the real `DriveNoteService` (in-memory note store) → `create_note` → the receipt rides the answer to
    `ConversationHandler`, which posts it literally; `drive_note_context` is one entry deep in `full_text`, and no
    note words enter history;
  - a `tell_alek` CALL: the gateway posts the receipt through `UserNotificationService` (literally, primary channel,
    call wording in the history pair);
  - a reminder run declares no writer.
Smart and the specialist are scripted LLMs. Synthetic data only: the bathroom-repair invoice."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.agents.drive_agent import DriveAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain import drive_note_tool_texts as nt
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentConfig, AgentResponse, DeliveryItem
from src.domain.drive_note import DRIVE_NOTE_RECEIPT_DELIVERY
from src.domain.drive_note_receipt import render_drive_note_receipt
from src.domain.language import LanguageCode
from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.delegation_engine import DelegationEngine
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.ports.agent_factory_port import AgentFactoryPort
from src.ports.language_service_port import LanguageServicePort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.support.scripted_llm import calls, script
from tests.unit.agents.test_drive_agent_note_tool_fields_e2e import World as NoteWorld
from tests.unit.agents.test_drive_agent_note_tool_fields_e2e import specialist
from tests.unit.services.test_drive_note_service import ACC, U, Env

REPAIR = "the bathroom repair invoice from Reformas Exemplo"
CHAT = {"user_id": U, "account_id": ACC, "session_id": f"{U}:C1", "turn_id": "turn-7", "interactive_delivery": True,
        "origin_channel_id": "C1", "origin_platform": "slack",
        "current_message_parts": [MessagePart(text="keep the bathroom repair invoice on the file")]}


class _Factory(AgentFactoryPort):
    def __init__(self, fake: SimpleNamespace) -> None:
        self.fake = fake

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        return await UserAgentFactory.create_agent_on_demand(self.fake, agent_type, user_id)


def _world(monkeypatch, env: Env):
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry, file_ref_resolver=AsyncMock(),
                                   task_queue=SimpleNamespace(enqueue_agent_task=AsyncMock(return_value="t")))
    prompts = AsyncMock()
    prompts.build_for_agent.return_value = f"DriveAgent extends Agent {{}}\n\n{PROMPT_CACHE_BOUNDARY}"
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    ctx = _UserContext(user_profile=profile, prompt_builder=prompts)
    fake = SimpleNamespace(
        _cache={U: {"_user_context": ctx, "_lazy_agent_ids": [], "last_used": time.time()}}, _creation_locks={},
        _LAZY_BUILDERS=UserAgentFactory._LAZY_BUILDERS, _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS,
        coordinator=coordinator, prompt_content_store=None, quota_service=None, context_builder=context_builder,
        file_conversion_service=MagicMock(), file_storage=MagicMock(), notification_service=None,
        user_drive_service=NoteWorld().operations._drive, localization=None, language_service=None,
        drive_search_service=None, drive_note_service=env.service, drive_notes_enabled=True, alert_sink=None)
    fake._drive_operations = lambda c: UserAgentFactory._drive_operations(fake, c)
    coordinator.set_agent_factory(_Factory(fake))
    llm, seen = script(calls((nt.CREATE_NOTE, {"item_ref": "drive:a", "text": REPAIR})),
                       calls((tt.REPORT_TOOL, {"note": "Kept the invoice on the file."})))

    async def specialist_call(self, request, turn=0, **kw):
        return await llm(request, turn)
    monkeypatch.setattr(DriveAgent, "_call_llm", specialist_call)
    return coordinator


def _handler() -> ConversationHandler:
    return ConversationHandler(coordinator=MagicMock(), agent_factory=MagicMock(), file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30),
                               localization=LocalizationService(FileLocalizationAdapter()))


def _channel() -> MagicMock:
    channel = MagicMock()
    channel.language = "en"
    channel.send_literal = AsyncMock()
    channel.send_message = AsyncMock()
    return channel


async def test_a_chat_turn_writes_the_note_and_the_receipt_is_posted_literally_with_one_level_of_history(monkeypatch):
    env = Env()
    coordinator = _world(monkeypatch, env)
    replies = [
        LLMResponse(text="", tool_calls=[ToolCall(name="delegate_to_specialist", thought_signature="s1", args={
            "intent": Intent.WORK_WITH_DRIVE_FILES, "query": "Keep the bathroom repair invoice on drive:a.", "context": {}})]),
        LLMResponse(text="Kept it on the file."),
    ]
    sent: list = []

    async def smart_llm(request: LLMRequest, turn: int = 0) -> LLMResponse:
        sent.append(request)
        return replies[len(sent) - 1]

    result = await DelegationEngine(coordinator=coordinator).execute(
        call_llm=smart_llm, base_request=LLMRequest(model_name="smart-model", messages=[
            Message(role="user", parts=[MessagePart(text="keep the bathroom repair invoice on the file")])]),
        context=dict(CHAT), max_turns=4, calling_agent_id=f"smart_response_agent_{U}")

    # the note is stored, by the real service, from a chat turn
    note = await env.note("a")
    assert note.text == REPAIR and note.current_version == 1
    # the receipt rides the answer
    receipts = [i for i in result.delivery_items if i.type == DRIVE_NOTE_RECEIPT_DELIVERY]
    assert len(receipts) == 1 and receipts[0].data["text"] == REPAIR
    # it is posted literally by ConversationHandler, in the shared renderer's text
    channel = _channel()
    await _handler()._deliver_drive_note_receipt(receipts[0], channel, None)
    channel.send_message.assert_not_awaited()
    (posted,) = [c.args[0] for c in channel.send_literal.await_args_list]
    assert posted.startswith("📝") and REPAIR in posted
    assert posted == render_drive_note_receipt(receipts[0].data, lambda m: _en(m))
    # the history: one drive_note_context entry per write, the line only; no note words anywhere
    entries = result.history_contexts["drive_note_context"]
    assert len(entries) == 1
    response = SimpleNamespace(metadata=dict(result.history_contexts))
    context = MessageContext(text="x", session_id=f"{U}:C1", user_id=U, account_id=ACC, metadata={})
    history_text, full_text = await _handler()._prepare_history_texts(response, "Kept it on the file.",
                                                                       "Kept it on the file.", None, context)
    block = json.loads(full_text.split("\n\n")[-1])
    assert block == {"drive_note_context": [["[Drive note: Docs/a.pdf ref=drive:a v1]"]]}   # the engine's fold: one entry
    assert REPAIR not in full_text and REPAIR not in history_text


def _en(message):
    from src.locales import en
    return en.UI_STRINGS[message.value]


async def test_a_tell_alek_call_posts_the_receipt_through_the_gateway_literally_with_call_wording():
    world = NoteWorld()
    response = await world.run(specialist([(nt.CREATE_NOTE, {"item_ref": "drive:a", "text": REPAIR})]),
                               run_origin="voice:tell_alek")
    items = [i for i in response.delivery_items if i.type == DRIVE_NOTE_RECEIPT_DELIVERY]
    assert len(items) == 1

    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = NotificationChannel(user_id=U, platform="slack", channel_id="D1",
                                                         updated_at=datetime(2026, 10, 28, tzinfo=timezone.utc))
    channel = _channel()
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    store = MagicMock()
    store.append_messages_batch = AsyncMock()
    language = AsyncMock(spec=LanguageServicePort)
    language.resolve_ui_language.return_value = LanguageCode.EN
    notifications = UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=MagicMock(), notification_sla={}, session_store=store,
        localization=LocalizationService(FileLocalizationAdapter()), language=language)
    gateway = AlekGatewayAgent(config=MagicMock(agent_id=f"alek_agent_{U}", timeout_ms=None),
                               notification_service=notifications)
    gateway.coordinator = MagicMock()
    gateway.coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart", result=SmartResponse(text="Kept it.", structured_data=None, link_list=[]),
        delivery_items=list(items)))
    from src.domain.agent import AgentIntent, AgentMessage
    message = AgentMessage.create(
        sender="coordinator", recipient=f"alek_agent_{U}", intent=AgentIntent.QUERY,
        payload={"query": "keep the bathroom repair invoice on the file", "intent": "tell_alek"},
        context={"user_id": U, "account_id": ACC, "session_id": f"{U}:D1"})

    await gateway.execute(message)

    (posted,) = [c.args[0] for c in channel.send_literal.await_args_list]
    assert REPAIR in posted
    user_turn = store.append_messages_batch.await_args.kwargs["messages"][0].parts[0].text
    assert "during a call with Lelik" in user_turn and REPAIR not in user_turn


async def test_a_reminder_run_declares_no_note_writer():
    world = NoteWorld()
    model = specialist()
    await world.run(model, interactive_delivery=None, run_origin="background:fire_due_reminders")
    declared = [t["name"] for t in model.requests[0].tools]
    assert nt.GET_NOTE in declared
    assert not {n for n in nt.NOTE_TOOLS if n != nt.GET_NOTE} & set(declared)

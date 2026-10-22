"""The chain (DRIVE_SPECIALIST_RFC §11.1 "Chain", review I7): a chat turn's context through Smart's engine to the
coordinator, which builds the drive specialist lazily through the factory's own `create_agent_on_demand`; the
specialist's DriveOperations sees the chat context (`interactive_delivery`, `session_id`, `turn_id`), a delete posts
its receipt to the chat the turn came from, an opened PDF reaches Smart's next model request as a vision file, and a
`drive:` ref from that answer reaches `create_pdf` through the coordinator's prefetch.

Fixture: the real registry and coordinator, the real factory methods on a stand-in factory object, the real
DriveAgent and DriveOperations over a spy drive service and a spy notifier; Smart and the specialist are scripted
LLMs (tests/support/scripted_llm.py). The Router is not in it: it routes every chat turn to Smart (its own tests)."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.agents.drive_agent import DriveAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain import drive_tool_texts as tt
from src.domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.domain.user import UserBotConfig
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.delegation_engine import DelegationEngine
from src.infrastructure.drive_operations import DriveOperations
from src.ports.agent_factory_port import AgentFactoryPort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import calls, script

USER, ACCOUNT = "u1", "a1"
CHAT = {"user_id": USER, "account_id": ACCOUNT, "session_id": f"{USER}:C1", "turn_id": "turn-7",
        "interactive_delivery": True, "origin_channel_id": "C1", "origin_platform": "slack",
        "current_message_parts": [MessagePart(text="delete the old notes and show me the scan")]}
SCAN = DriveItem(item_id="scan", name="Scan_0012.pdf", path="Scans/Scan_0012.pdf", is_folder=False,
                 mime_type="application/pdf", size_bytes=1000)
OLD = DriveItem(item_id="old", name="old.txt", path="Inbox/old.txt", is_folder=False)


def _delegate(n: int, intent: str, query: str, **ctx: str) -> ToolCall:
    return ToolCall(name="delegate_to_specialist", args={"intent": intent, "query": query, "context": ctx},
                    thought_signature=f"s{n}")


class _Factory(AgentFactoryPort):
    """The coordinator's factory port, served by UserAgentFactory's own lazy path on a stand-in factory."""

    def __init__(self, fake: SimpleNamespace) -> None:
        self.fake = fake
        self.asked: list = []

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        self.asked.append(agent_type)
        return await UserAgentFactory.create_agent_on_demand(self.fake, agent_type, user_id)


def _world(monkeypatch):
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    resolver = AsyncMock(return_value="SCAN TEXT")
    queue = SimpleNamespace(enqueue_agent_task=AsyncMock(return_value="task-1"))
    coordinator = AgentCoordinator(registry=registry, file_ref_resolver=resolver, task_queue=queue)

    drive = AsyncMock(spec=UserDriveService)
    drive.connection.return_value = DriveConnection(connected=True)
    drive.delete.return_value = SimpleNamespace(item=OLD)
    conversion = MagicMock(spec=FileConversionService)
    conversion.get_drive_item = AsyncMock(return_value=SCAN)
    conversion.resolve_bytes = AsyncMock(return_value=b"%PDF-1.4 scan")
    notifier = SimpleNamespace(notify_raw=AsyncMock(), notify_file_bytes=AsyncMock())
    localization = MagicMock()
    localization.get_ui_string.return_value = "Deleted {path}"
    prompts = AsyncMock()
    prompts.build_for_agent.return_value = f"DriveAgent extends Agent {{}}\n\n{PROMPT_CACHE_BOUNDARY}"
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    ctx = _UserContext(user_profile=profile, prompt_builder=prompts)
    fake = SimpleNamespace(
        _cache={USER: {"_user_context": ctx, "_lazy_agent_ids": [], "last_used": time.time()}},
        _creation_locks={}, _LAZY_BUILDERS=UserAgentFactory._LAZY_BUILDERS,
        _LAZY_AGENT_IDS=UserAgentFactory._LAZY_AGENT_IDS, coordinator=coordinator, prompt_content_store=None,
        quota_service=None, context_builder=context_builder, file_conversion_service=conversion,
        file_storage=MagicMock(), notification_service=notifier, user_drive_service=drive,
        localization=localization, language_service=None, drive_search_service=None, alert_sink=None)
    fake._drive_operations = lambda c: UserAgentFactory._drive_operations(fake, c)
    factory = _Factory(fake)
    coordinator.set_agent_factory(factory)

    seen_contexts: list = []
    real_delete = DriveOperations.delete

    async def delete_seen(self, context, user_id, ref, *, wait_s):
        seen_contexts.append(dict(context))
        return await real_delete(self, context, user_id, ref, wait_s=wait_s)
    monkeypatch.setattr(DriveOperations, "delete", delete_seen)

    specialist_llm, specialist_seen = script(
        calls((tt.DELETE_DRIVE_ITEM, {"file_ref": "drive:old"}), (tt.OPEN_DRIVE_FILE, {"file_ref": "drive:scan"})),
        calls((tt.REPORT_TOOL, {"note": "Deleted the old notes and opened the scan."})))

    async def specialist_call(self, request, turn=0, **kw):
        return await specialist_llm(request, turn)
    monkeypatch.setattr(DriveAgent, "_call_llm", specialist_call)
    return SimpleNamespace(coordinator=coordinator, factory=factory, fake=fake, ctx=ctx, drive=drive,
                           notifier=notifier, resolver=resolver, queue=queue, seen_contexts=seen_contexts,
                           specialist_seen=specialist_seen)


async def test_a_chat_turn_reaches_the_lazy_specialist_and_back_with_receipts_vision_and_prefetch(monkeypatch):
    world = _world(monkeypatch)
    smart_requests: list = []
    replies = [
        LLMResponse(text="", tool_calls=[_delegate(1, Intent.WORK_WITH_DRIVE_FILES,
                                                   "Delete Inbox/old.txt (drive:old) and open the scan (drive:scan).")]),
        LLMResponse(text="", tool_calls=[_delegate(2, Intent.CREATE_PDF, "A PDF of the scan.", file_ref="drive:scan")]),
        LLMResponse(text="Done: the notes are deleted, the PDF is on its way."),
    ]

    async def smart_llm(request: LLMRequest, turn: int = 0) -> LLMResponse:
        smart_requests.append(request)
        return replies[len(smart_requests) - 1]

    result = await DelegationEngine(coordinator=world.coordinator).execute(
        call_llm=smart_llm, base_request=LLMRequest(model_name="smart-model", messages=[
            Message(role="user", parts=[MessagePart(text="delete the old notes and show me the scan")])]),
        context=dict(CHAT), max_turns=5, calling_agent_id=f"smart_response_agent_{USER}")

    # the coordinator built the specialist lazily, through the factory, on the user's shared DriveOperations
    agent = world.coordinator.get_agent(f"drive_agent_{USER}")
    assert isinstance(agent, DriveAgent) and "drive" in world.factory.asked
    assert agent.agent_id in world.fake._cache[USER]["_lazy_agent_ids"]
    assert agent.operations is world.ctx.drive_operations
    # the specialist's DriveOperations saw the chat context
    (seen,) = world.seen_contexts
    assert (seen["interactive_delivery"], seen["session_id"], seen["turn_id"]) == (True, f"{USER}:C1", "turn-7")
    assert 'user_message (chat): "delete the old notes and show me the scan"' in \
        world.specialist_seen[0].system_instruction
    # the delete really ran (no dry run here) and its receipt went to the chat the turn came from
    world.drive.delete.assert_awaited_once_with(USER, "old")
    world.notifier.notify_raw.assert_awaited_once_with(USER, ACCOUNT, "Deleted Inbox/old.txt",
                                                       channel_id_override="C1", platform_override="slack")
    # the opened PDF reached Smart's next request as a vision file, after the specialist's answer
    files = [p.file_data for m in smart_requests[1].messages for p in m.parts if p.file_data]
    assert [f["mime_type"] for f in files] == ["application/pdf"]
    answer = next(p.tool_response["response"]["result"] for m in smart_requests[1].messages for p in m.parts
                  if p.tool_response)
    assert answer.startswith("Drive result: ok")
    # the card's drive: ref reached create_pdf through the coordinator's prefetch, before the enqueue
    world.resolver.assert_awaited_once_with("drive:scan", USER)
    enqueued = world.queue.enqueue_agent_task.await_args.kwargs
    assert enqueued["intent"] == Intent.CREATE_PDF
    assert enqueued["context"]["params"]["file_content"] == "SCAN TEXT"
    assert result.text.startswith("Done:")

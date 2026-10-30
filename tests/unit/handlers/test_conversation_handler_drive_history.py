"""The drive's history lines in the session pair (DRIVE_SPECIALIST_RFC §7.4): ConversationHandler writes one readable
line per file into the model message's full_text, never into its summary or the consolidation text; the next turn
sees the line while the turn is recent, and the ref copied from it resolves through DriveOperations. Other `*_context`
keys keep their JSON block. Invented items only."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.base_agent import BaseAgent
from src.domain import drive_answer_texts as t
from src.domain.agent import AgentConfig, AgentResponse
from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_pass_through import PassThroughCollector
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, MoveOutcome
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_service_connections import ConnectionReads
from tests.unit.domain.drive_excerpt_builders import chunk, excerpt_file, result

LAB = DriveItem(item_id="abc123", name="lab-report.pdf", path="Health/lab-report.pdf", is_folder=False)
LEASE = DriveItem(item_id="def456", name="Lisbon lease.pdf", path="Home/Lisbon lease.pdf", is_folder=False)
LAB_LINE = '[Drive: Health/lab-report.pdf ref=drive:abc123 — searched for "lab report" (2 files); read passages 3–4]'
LEASE_LINE = '[Drive: Home/Lisbon lease.pdf ref=drive:def456 — searched for "lab report" (2 files)]'


def _drive_context():
    """One commission's entries as Smart's engine folds them: search, then passages of the lab report (3 entries)."""
    c = PassThroughCollector()
    c.add(1, t.KIND_SEARCH, DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="cards", success=True,
                                         items=(LAB, LEASE), request_text="lab report", ref_tag="a1"))
    f = excerpt_file(LAB.item_id, LAB.path, [chunk(LAB.item_id, 2, "a"), chunk(LAB.item_id, 3, "b")])
    c.add(2, t.KIND_PASSAGES, DriveOutcome(op=DriveOp.PASSAGES, cls=OutcomeClass.OK, text="p", success=True,
                                           items=(LAB,), excerpts=result(f, question="LAB-2025-03-0417 values"),
                                           ref_tag="a1"))
    return [c.drive_context()]


def _handler(response):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    return ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30)), session_store


def _channel():
    ch = MagicMock()
    ch.channel_id, ch.platform, ch.max_message_length, ch.supports_message_editing = "C-001", "slack", 4000, True
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    return ch


async def _stored_pair(metadata):
    response = AgentResponse.success(task_id="t", agent_id="smart",
                                     result=SmartResponse(text="Your lab report shows the values."))
    response.metadata["response_summary"] = "Read the lab report."
    response.metadata.update(metadata)
    handler, store = _handler(response)
    ctx = MessageContext(text="what does my lab report say?", session_id="s", user_id="u", account_id="a",
                         attachments=[], metadata={})
    with patch.object(handler, "validate_model_output", side_effect=lambda text, u: text):
        await handler.handle_message(ctx, _channel())
    return store.append_messages_batch.call_args[0][1]


async def test_full_text_holds_one_line_per_file_and_no_json():
    messages = await _stored_pair({"drive_context": _drive_context()})
    model = next(m for m in messages if m.role == "model")
    assert model.parts[0].full_text == f"Your lab report shows the values.\n\n{LAB_LINE}\n{LEASE_LINE}"
    assert '"drive_context"' not in model.parts[0].full_text


async def test_the_summary_and_the_consolidation_text_never_hold_the_lines():
    messages = await _stored_pair({"drive_context": _drive_context()})
    model = next(m for m in messages if m.role == "model")
    assert model.parts[0].text == "Read the lab report."
    consolidated = json.dumps(serialize_messages_for_consolidation(messages), ensure_ascii=False, default=str)
    assert "drive:" not in consolidated and "[Drive:" not in consolidated


async def test_an_empty_drive_context_adds_nothing_and_other_context_keys_keep_their_json():
    web = {"query": "lab report units", "result": "mmol/L"}
    messages = await _stored_pair({"drive_context": [[{"op": "listed"}]], "web_search_context": [web]})
    model = next(m for m in messages if m.role == "model")
    block = json.dumps({"web_search_context": [web]}, ensure_ascii=False, separators=(",", ":"))
    assert model.parts[0].full_text == "Your lab report shows the values.\n\n" + block


class _Agent(BaseAgent):
    async def can_handle(self, message):
        return True

    async def execute(self, message):
        return SimpleNamespace()


async def test_the_next_turn_sees_the_line_and_its_ref_resolves_through_drive_operations():
    messages = await _stored_pair({"drive_context": _drive_context()})
    agent = _Agent(AgentConfig(agent_id="smart_response_agent_u1", agent_type="smart_response"))
    recent = agent._apply_history_tier(messages, max_full_turns=5)
    seen = recent[1].parts[0].text
    assert LAB_LINE in seen
    ref = next(w for w in LAB_LINE.split() if w.startswith("ref="))[len("ref="):]   # what Smart copies

    drive = AsyncMock(spec=UserDriveService)
    drive.resolve_ref.side_effect = ConnectionReads().resolve_ref
    moved = DriveItem(item_id="abc123", name="lab-report.pdf", path="Archive/lab-report.pdf", is_folder=False)

    async def move(handle, item_id, folder, new_name):
        if item_id != LAB.item_id:
            raise DriveItemNotFoundError(item_id)
        return MoveOutcome(before=LAB, after=moved, created=[])
    drive.move.side_effect = move
    search = AsyncMock(spec=DriveSearchService)
    search.access_note.return_value = None
    ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), notification=AsyncMock(),
                          drive_service=drive, localization=MagicMock(), language_service=AsyncMock(),
                          drive_search=search)
    for given in (ref, LAB_LINE):   # the ref alone, or the whole line pasted (lenient parse)
        out = await ops.move({"user_id": "u1"}, "u1", given, folder="Archive", wait_s=5)
        assert out.success and "Archive/lab-report.pdf" in out.text

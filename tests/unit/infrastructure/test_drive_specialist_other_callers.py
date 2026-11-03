"""The drive's other callers after the move (DRIVE_SPECIALIST_RFC §10 "Unchanged callers"): the generators still get a
`drive:` ref from a card prefetched by the coordinator (FileConversionService resolves it), the drive specialist and
FileManagement never are, and Lelik reaches the drive only through Alek → Router → Smart."""
from unittest.mock import AsyncMock

from src.domain.document_open import Prefetched
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import (
    ALEK, ALL_DESCRIPTORS, DRIVE_SPECIALIST, FILE_MANAGEMENT, LELIK, PDF_GENERATOR, Intent)
from src.infrastructure.agent_registry import AgentRegistry


def _coordinator(resolver) -> AgentCoordinator:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return AgentCoordinator(registry=registry, file_ref_resolver=resolver)


# The prefetch is `prefetch_file_ref` since P18 (EXTERNAL_FILES_RFC §4.2): the resolver takes the account and an
# absolute deadline and answers a Prefetched.
CTX = {"user_id": "u1", "account_id": "a1"}
DEADLINE = 1_900_000_000.0


async def test_a_generator_still_gets_a_drive_ref_prefetched():
    resolver = AsyncMock(return_value=Prefetched(content="lease text"))
    params = {"file_ref": "drive:AAA"}
    await _coordinator(resolver).prefetch_file_ref(PDF_GENERATOR.agent_id, params, CTX, deadline_at=DEADLINE)
    resolver.assert_awaited_once_with("drive:AAA", "u1", "a1", DEADLINE)
    assert params["file_content"] == "lease text"


async def test_the_specialist_and_file_management_read_refs_themselves():
    resolver = AsyncMock(return_value=Prefetched(content="never"))
    coordinator = _coordinator(resolver)
    for agent_id in (DRIVE_SPECIALIST.agent_id, FILE_MANAGEMENT.agent_id):
        params = {"file_ref": "drive:AAA"}
        await coordinator.prefetch_file_ref(agent_id, params, CTX, deadline_at=DEADLINE)
        assert "file_content" not in params and "file_note" not in params
    resolver.assert_not_awaited()


def test_lelik_reaches_the_drive_only_through_alek():
    assert Intent.WORK_WITH_DRIVE_FILES not in LELIK.allowed_intents
    # A question or an action goes through ask_alek (allowlisted); the chat errand through Lelik's send_to_chat tool,
    # which runs as the gateway's own intent (VOICE_COMPANION_RFC §4.15.2, revised 2026-10-29).
    assert Intent.ASK_ALEK in LELIK.allowed_intents
    assert set(ALEK.capabilities) == {Intent.ASK_ALEK, Intent.SEND_TO_CHAT}
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    reachable = {registry.get_agent_for_intent(i).agent_id for i in LELIK.allowed_intents | {Intent.SEND_TO_CHAT}}
    assert DRIVE_SPECIALIST.agent_id not in reachable and FILE_MANAGEMENT.agent_id not in reachable
    assert registry.get_agent_for_intent(Intent.SEND_TO_CHAT) is ALEK

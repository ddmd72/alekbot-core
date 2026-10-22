"""The drive's other callers after the move (DRIVE_SPECIALIST_RFC §10 "Unchanged callers"): the generators still get a
`drive:` ref from a card prefetched by the coordinator (FileConversionService resolves it), the drive specialist and
FileManagement never are, and Lelik reaches the drive only through Alek → Router → Smart."""
from unittest.mock import AsyncMock

from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import (
    ALEK, ALL_DESCRIPTORS, DRIVE_SPECIALIST, FILE_MANAGEMENT, LELIK, PDF_GENERATOR, Intent)
from src.infrastructure.agent_registry import AgentRegistry


def _coordinator(resolver) -> AgentCoordinator:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    return AgentCoordinator(registry=registry, file_ref_resolver=resolver)


async def test_a_generator_still_gets_a_drive_ref_prefetched():
    resolver = AsyncMock(return_value="lease text")
    params = {"file_ref": "drive:AAA"}
    await _coordinator(resolver)._maybe_resolve_file_refs(PDF_GENERATOR.agent_id, params, "u1")
    resolver.assert_awaited_once_with("drive:AAA", "u1")
    assert params["file_content"] == "lease text"


async def test_the_specialist_and_file_management_read_refs_themselves():
    resolver = AsyncMock(return_value="never")
    coordinator = _coordinator(resolver)
    for agent_id in (DRIVE_SPECIALIST.agent_id, FILE_MANAGEMENT.agent_id):
        params = {"file_ref": "drive:AAA"}
        await coordinator._maybe_resolve_file_refs(agent_id, params, "u1")
        assert "file_content" not in params
    resolver.assert_not_awaited()


def test_lelik_reaches_the_drive_only_through_alek():
    assert Intent.WORK_WITH_DRIVE_FILES not in LELIK.allowed_intents
    assert {Intent.ASK_ALEK, Intent.TELL_ALEK} <= LELIK.allowed_intents
    assert set(ALEK.capabilities) == {Intent.ASK_ALEK, Intent.TELL_ALEK}

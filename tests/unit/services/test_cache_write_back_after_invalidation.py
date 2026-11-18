"""P15-S2 beyond the two pins: every invalidation path, not only `bind()` and `invalidate_cache()`, keeps a read that
straddled it from writing its old value back into the cache.

The pins are `test_channel_binding_interleaving.py` (a get straddling a bind) and `test_prompt_cache_interleaving.py`
(an assembly straddling `invalidate_cache()`). Each test here forces the straddle with a gate and asserts it happened.
"""
import asyncio
from typing import Dict, Optional
from unittest.mock import Mock

from src.domain.channel_binding import ChannelBinding
from src.ports.channel_binding_port import ChannelBindingPort
from src.services.channel_binding_service import ChannelBindingService
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService

GATE_TIMEOUT_S = 5


class _GatedPort(ChannelBindingPort):
    """Stores bindings; the first get captures the stored value, then waits on the gate."""

    def __init__(self, stored: Dict[str, ChannelBinding]) -> None:
        self.stored = stored
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.reads = 0
        self._gate_next_get = True

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        self.reads += 1
        captured = self.stored.get(channel_id)
        if self._gate_next_get:
            self._gate_next_get = False
            self.reached.set()
            await self.release.wait()
        return captured

    async def save(self, binding: ChannelBinding) -> None:
        self.stored[binding.channel_id] = binding

    async def delete(self, channel_id: str) -> None:
        self.stored.pop(channel_id, None)


def _binding(agent_type: str) -> ChannelBinding:
    return ChannelBinding(channel_id="C1", agent_type=agent_type, intent="chat", created_by="u1")


async def _straddle(service: ChannelBindingService, port: _GatedPort, change) -> Optional[ChannelBinding]:
    reader = asyncio.create_task(service.get("C1"))
    await asyncio.wait_for(port.reached.wait(), GATE_TIMEOUT_S)
    await change()
    port.release.set()
    return await reader


class TestChannelBindingWriteBack:

    async def test_a_get_straddling_an_unbind_does_not_bring_the_binding_back(self):
        port = _GatedPort({"C1": _binding("tutor")})
        service = ChannelBindingService(port)

        straddled = await _straddle(service, port, lambda: service.unbind("C1"))

        assert port.reached.is_set()
        assert straddled == _binding("tutor")  # the in-flight read answers what it read
        assert await service.get("C1") is None

    async def test_a_get_straddling_an_invalidate_reads_the_store_again(self):
        port = _GatedPort({"C1": _binding("tutor")})
        service = ChannelBindingService(port)

        async def change():
            port.stored["C1"] = _binding("lelik")  # written by another instance
            service.invalidate("C1")

        await _straddle(service, port, change)

        assert port.reached.is_set()
        assert await service.get("C1") == _binding("lelik")
        assert port.reads == 2

    async def test_a_get_with_nothing_in_between_is_cached(self):
        port = _GatedPort({"C1": _binding("tutor")})
        port._gate_next_get = False
        service = ChannelBindingService(port)

        await service.get("C1")
        await service.get("C1")

        assert port.reads == 1


def _assembly(source: dict):
    service = PromptAssemblyService(
        token_repo=Mock(), blueprint_repo=Mock(), profile_repo=Mock(), security_port=Mock(),
        formatter=Mock(), bio_formatter=Mock(), cache_ttl=86400,
    )
    gate = {"reached": asyncio.Event(), "release": asyncio.Event(), "armed": True, "reads": 0}

    async def gated_template(agent_type, account_id, user_id):
        gate["reads"] += 1
        seen = source[agent_type]
        if gate["armed"]:
            gate["armed"] = False
            gate["reached"].set()
            await gate["release"].wait()
        return seen

    async def passthrough(static_prompt, *args, **kwargs):
        return static_prompt

    service._assemble_static_template = gated_template  # type: ignore[method-assign]
    service._inject_runtime_context = passthrough  # type: ignore[method-assign]
    return service, gate


class TestPromptAssemblyWriteBack:

    async def test_an_assembly_straddling_invalidate_assembled_is_not_served_afterwards(self):
        source = {"smart": "OLD"}
        service, gate = _assembly(source)

        first = asyncio.create_task(service.assemble("smart", user_id="u1", account_id="a1"))
        await asyncio.wait_for(gate["reached"].wait(), GATE_TIMEOUT_S)
        source["smart"] = "NEW"
        service.invalidate_assembled("smart", account_id="a1", user_id="u1")
        gate["release"].set()

        assert await first == "OLD"
        assert await service.assemble("smart", user_id="u1", account_id="a1") == "NEW"

    async def test_an_assembly_with_no_invalidation_in_between_is_cached(self):
        source = {"smart": "ONLY"}
        service, gate = _assembly(source)
        gate["armed"] = False

        await service.assemble("smart", user_id="u1", account_id="a1")
        await service.assemble("smart", user_id="u1", account_id="a1")

        assert gate["reads"] == 1

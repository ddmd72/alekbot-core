"""S2 (channel binding): a cache-miss ``get`` that straddles a ``bind`` of the same channel.

``get`` reads the port, then writes the cache; ``bind`` saves and writes the cache. If the
read began before the save, the old value overwrites the new one in the cache for the TTL.
The fake port captures its stored value at call time and suspends on a gate.

``PromptComponentService.get_assembled_prompt`` is not tested: it has no caller in ``src/``.
"""

import asyncio
from typing import Dict, Optional

import pytest

from src.domain.channel_binding import ChannelBinding
from src.ports.channel_binding_port import ChannelBindingPort
from src.services.channel_binding_service import ChannelBindingService


class GatedBindingPort(ChannelBindingPort):
    def __init__(self, stored: Dict[str, ChannelBinding]) -> None:
        self.stored = stored
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self._gate_next_get = True

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        captured = self.stored.get(channel_id)  # captured at call time
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


@pytest.mark.asyncio
@pytest.mark.parametrize("old", [_binding("tutor"), None], ids=["old-binding", "unbound"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="P15-S2: a get that read the port before bind() overwrites the fresh cache entry "
    "with the old value for the cache TTL; owner decision",
)
async def test_get_started_before_bind_does_not_overwrite_the_new_binding(old):
    port = GatedBindingPort({"C1": old} if old else {})
    service = ChannelBindingService(port)
    new = _binding("lelik")

    reader = asyncio.create_task(service.get("C1"))
    await asyncio.wait_for(port.reached.wait(), timeout=5)
    await service.bind(new)
    port.release.set()
    await reader
    assert port.reached.is_set()

    assert await service.get("C1") == new

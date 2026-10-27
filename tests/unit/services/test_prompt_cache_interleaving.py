"""S2 (prompt assembly): a cache miss that is mid-assembly when the cache is invalidated.

``assemble`` misses, awaits ``_assemble_static_template`` and then ``_save_to_cache``;
``invalidate_cache()`` takes no key and clears everything. A template read before the
invalidation but saved after it is stale and gets served for the full TTL.

No Firestore collection is read here: the gated coroutine is the only suspension point.
The gate captures the source value at call time, so the test is not vacuous.
"""

import asyncio
from unittest.mock import Mock

import pytest

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService


def _service(source: dict) -> tuple[PromptAssemblyService, asyncio.Event, asyncio.Event]:
    service = PromptAssemblyService(
        token_repo=Mock(),
        blueprint_repo=Mock(),
        profile_repo=Mock(),
        security_port=Mock(),
        formatter=Mock(),
        bio_formatter=Mock(),
        cache_ttl=86400,
    )
    reached = asyncio.Event()
    release = asyncio.Event()

    async def gated_template(agent_type, account_id, user_id):
        seen = source["template"]  # captured at call time
        reached.set()
        await release.wait()
        return seen

    async def passthrough(static_prompt, *args, **kwargs):
        return static_prompt

    service._assemble_static_template = gated_template  # type: ignore[method-assign]
    service._inject_runtime_context = passthrough  # type: ignore[method-assign]
    return service, reached, release


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="P15-S2: assemble saves a template read before invalidate_cache() after the "
    "invalidation, so the stale prompt is served for the cache TTL; owner decision",
)
async def test_template_read_before_invalidation_is_not_served_afterwards():
    source = {"template": "OLD"}
    service, reached, release = _service(source)

    first = asyncio.create_task(service.assemble("smart", user_id="u1", account_id="a1"))
    await asyncio.wait_for(reached.wait(), timeout=5)
    source["template"] = "NEW"
    service.invalidate_cache()
    release.set()
    await first
    assert reached.is_set()

    second = await service.assemble("smart", user_id="u1", account_id="a1")
    assert second == "NEW"

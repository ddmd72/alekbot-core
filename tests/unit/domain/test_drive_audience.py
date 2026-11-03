"""The drive audience ContextVar (DRIVE_MCP_CONNECTOR_RFC §7.2): unset by default, set only inside the block, inherited
by a task created inside it and not by one created before."""
import asyncio

from src.domain.drive_audience import CURRENT_DRIVE_AUDIENCE, DriveAudience, connector_audience


def test_unset_by_default():
    assert CURRENT_DRIVE_AUDIENCE.get() is None


def test_connector_audience_sets_then_resets():
    with connector_audience():
        assert CURRENT_DRIVE_AUDIENCE.get() is DriveAudience.CONNECTOR
    assert CURRENT_DRIVE_AUDIENCE.get() is None
    try:
        with connector_audience():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert CURRENT_DRIVE_AUDIENCE.get() is None


async def test_a_task_created_inside_inherits_it():
    async def seen():
        return CURRENT_DRIVE_AUDIENCE.get()

    with connector_audience():
        task = asyncio.create_task(seen())
    assert await task is DriveAudience.CONNECTOR
    assert CURRENT_DRIVE_AUDIENCE.get() is None


async def test_a_task_created_before_does_not_see_it():
    gate = asyncio.Event()

    async def seen():
        await gate.wait()
        return CURRENT_DRIVE_AUDIENCE.get()

    task = asyncio.create_task(seen())
    with connector_audience():
        gate.set()
        result = await task
    assert result is None

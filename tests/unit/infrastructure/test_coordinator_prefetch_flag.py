"""prefetch_file_ref (docs/10_rfcs/USER_DRIVE_RFC.md §4.4)."""
import logging
from unittest.mock import AsyncMock

from src.domain.user_drive import DriveNotConnectedError
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode


def _coordinator(prefetch: bool):
    registry = AgentRegistry()
    registry.register(AgentDescriptor(agent_id="files", agent_type="files",
                                      capabilities={"open_x": ExecutionMode.SYNC}, prefetch_file_ref=prefetch))
    coord = AgentCoordinator(registry=registry)
    coord._file_ref_resolver = AsyncMock(return_value="text")
    return coord


async def test_default_still_prefetches():
    coord = _coordinator(True)
    params = {"file_ref": "a.txt"}
    await coord._maybe_resolve_file_refs("files", params, "u1")
    assert params["file_content"] == "text"


async def test_flag_off_skips_prefetch():
    coord = _coordinator(False)
    params = {"file_ref": "drive:abc"}
    await coord._maybe_resolve_file_refs("files", params, "u1")
    coord._file_ref_resolver.assert_not_called()
    assert "file_content" not in params


async def test_unknown_agent_keeps_old_behaviour():
    coord = _coordinator(False)
    params = {"file_ref": "a.txt"}
    await coord._maybe_resolve_file_refs("other", params, "u1")
    assert params["file_content"] == "text"


async def test_drive_not_connected_is_a_warning_not_an_error(caplog):
    coord = _coordinator(True)
    coord._file_ref_resolver = AsyncMock(side_effect=DriveNotConnectedError("not connected"))
    params = {"file_ref": "drive:abc"}
    with caplog.at_level(logging.DEBUG):
        await coord._maybe_resolve_file_refs("files", params, "u1")
    assert "file_content" not in params
    records = [r for r in caplog.records if "drive:abc" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)


async def test_other_resolver_errors_still_logged_at_error(caplog):
    """Only the not-connected state is downgraded; every other failure keeps its ERROR line."""
    coord = _coordinator(True)
    coord._file_ref_resolver = AsyncMock(side_effect=RuntimeError("boom"))
    params = {"file_ref": "a.txt"}
    with caplog.at_level(logging.DEBUG):
        await coord._maybe_resolve_file_refs("files", params, "u1")
    assert "file_content" not in params
    records = [r for r in caplog.records if "a.txt" in r.getMessage()]
    assert records and all(r.levelno == logging.ERROR for r in records)


# Both dispatch paths must consult the flag (RFC §4.4 names _execute_sync AND _execute_async).

async def test_sync_path_honours_flag():
    coord = _coordinator(False)
    context = {"user_id": "u1", "params": {"file_ref": "drive:abc"}}
    await coord._execute_sync("files", "open_x", "open it", context)
    coord._file_ref_resolver.assert_not_called()
    assert "file_content" not in context["params"]


async def test_async_path_honours_flag():
    coord = _coordinator(False)
    coord._task_queue = AsyncMock()
    coord._task_queue.enqueue_agent_task = AsyncMock(return_value="task-1")
    context = {"user_id": "u1", "params": {"file_ref": "drive:abc"}}
    resp = await coord._execute_async("files", "open_x", "open it", context)
    coord._file_ref_resolver.assert_not_called()
    assert "file_content" not in context["params"]
    assert resp.result["task_name"] == "task-1"
    coord._task_queue.enqueue_agent_task.assert_awaited_once()


async def test_async_path_default_still_prefetches():
    coord = _coordinator(True)
    coord._task_queue = AsyncMock()
    coord._task_queue.enqueue_agent_task = AsyncMock(return_value="task-1")
    context = {"user_id": "u1", "params": {"file_ref": "a.txt"}}
    await coord._execute_async("files", "open_x", "open it", context)
    coord._file_ref_resolver.assert_awaited_once_with("a.txt", "u1")
    assert context["params"]["file_content"] == "text"

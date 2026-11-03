"""Mutation-survivor test (group F) for drive_tools: a malformed page request is handed to the operation."""
from src.domain import drive_tool_texts as tt
from tests.unit.infrastructure.test_drive_tools_open_bound import _call, _refusal, _run


async def test_a_malformed_page_is_answered_by_the_operation_not_dropped():
    ops, run, collector = _run(_refusal())
    seen = await _call(run, tt.READ_PASSAGES, file_ref="drive:a1:item-1", passages="from 0")
    assert seen is not None
    assert ops.read_passages.await_count == 1
    assert ops.read_passages.await_args.kwargs["passages"] == "from 0"
    assert len(collector.results) == 1

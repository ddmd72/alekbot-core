"""The connector contract's §14 delta (DRIVE_MCP_CONNECTOR_RFC §14.2, §14.4, §14.6): `wide` on `search_drive` and
`find_passages`, `batch` on `find_passages` only, the chat tools' parameters otherwise mirrored, and the paging lines'
result keys with the next-batch line's placeholders."""
from src.domain import drive_connector_contract as c
from src.domain.drive_tool_texts import FIND_PASSAGES, READ_PASSAGES, SEARCH_DRIVE


def test_wide_and_batch_are_the_connectors_own_parameters():
    assert c.TOOL_PARAMETERS[SEARCH_DRIVE] == ("query", "folder", "drives", "kind", "date_from", "date_to", "wide")
    assert c.TOOL_PARAMETERS[FIND_PASSAGES] == ("question", "folder", "drives", "file_refs", "wide", "batch")
    assert "batch" not in c.TOOL_PARAMETERS[SEARCH_DRIVE]
    assert all("wide" not in params and "batch" not in params for tool, params in c.TOOL_PARAMETERS.items()
               if tool not in (SEARCH_DRIVE, FIND_PASSAGES))
    assert c.TOOL_PARAMETERS[READ_PASSAGES] == ("file_ref", "passages")


def test_their_texts_and_the_paging_lines_are_required_keys():
    assert {"search_drive.wide", "find_passages.wide", "find_passages.batch"} <= c.REQUIRED_TOOL_KEYS
    assert set(c.PAGING_KEYS) == {c.BATCH_NEXT, c.BATCH_NEXT_WIDE, c.BATCH_END, c.SEARCH_MORE, c.SEARCH_MORE_WIDE}
    assert set(c.PAGING_KEYS) <= c.REQUIRED_RESULT_KEYS


def test_only_the_next_batch_line_is_formatted():
    assert c.RESULT_PLACEHOLDERS[c.BATCH_NEXT] == {"batch", "arguments"}
    assert all(key not in c.RESULT_PLACEHOLDERS for key in c.PAGING_KEYS if key != c.BATCH_NEXT)

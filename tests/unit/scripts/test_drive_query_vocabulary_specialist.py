"""The vocabulary probe after the move (DRIVE_SPECIALIST_RFC §10 step 5): the drive specialist's own `search_drive` and
`open_drive_file` tool calls are read like Smart's old delegations, in the same trace, so a window across the deploy
measures both; its query lives in `query`, its open in `file_ref`."""
import json

from scripts.validation import drive_query_vocabulary as script
from scripts.validation.drive_query_vocabulary_logic import INTENT_OPEN, INTENT_SEARCH, extract_events, pair_searches


def _row(trace, ts, *calls):
    return {"trace_id": trace, "timestamp": ts, "tool_calls": json.dumps(list(calls))}


def _tool(name, **args):
    return {"name": name, "args": args}


ROWS = [
    _row("tr_s", "2026-11-02T10:00:00Z", _tool("search_drive", query="Factura FA-0137", folder="Home")),
    _row("tr_s", "2026-11-02T10:00:04Z", _tool("open_drive_file", file_ref="drive:FK"),
         _tool("report_to_orchestrator", note="done")),
    _row("tr_s", "2026-11-02T10:00:09Z", _tool("search_drive", query="")),
    # Smart's own delegation of the specialist is not a search
    _row("tr_s", "2026-11-02T09:59:58Z", {"name": "delegate_to_specialist",
                                          "args": {"intent": "work_with_drive_files", "query": "find the invoice"}}),
]


def test_the_specialists_searches_and_opens_are_events():
    events = extract_events(ROWS)
    assert [(e.kind, e.drive_query, e.item_id) for _, e in events] == [
        (INTENT_SEARCH, "Factura FA-0137", ""), (INTENT_OPEN, "", "FK"), (INTENT_SEARCH, "", "")]


def test_an_open_pairs_with_the_specialists_search_before_it():
    records = pair_searches(ROWS)
    assert [(r.drive_query, r.opened) for r in records] == [("Factura FA-0137", ("FK",)), ("", ())]


def test_the_query_reads_smarts_and_the_specialists_rows():
    sql = script.build_query("p.d.t", with_until=False)
    assert "agent_type = 'drive'" in sql and "agent_type LIKE '%smart%'" in sql
    for name in ("search_files_in_drive", "open_file_from_drive", "search_drive", "open_drive_file"):
        assert f"'%{name}%'" in sql, name

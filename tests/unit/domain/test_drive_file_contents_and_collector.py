"""`file_contents` items bound by code (DRIVE_SPECIALIST_RFC §4.2, §5.3), the collector's search memo (§5.4) and the
preflight lines (§5.3)."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_file_content_item import FileContentItem
from src.domain.drive_file_contents import parse_file_contents
from src.domain.drive_index import MIGRATION_GATE_KEY, DriveSyncState, EpochPhase
from src.domain.drive_line import DriveLine
from src.domain.drive_pass_through import PassThroughCollector, search_key
from src.domain.drive_search_line import SearchLine, search_line
from src.domain.user_drive import DriveConnection

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def test_items_get_handles_in_order_with_name_or_target():
    items = parse_file_contents({"file_contents": [
        {"name": "a.md", "text": "first\nsecond"}, {"target": "drive:x", "text": "more"}, {"text": ""}]})
    assert items == (FileContentItem("t1", "first\nsecond", name="a.md"),
                     FileContentItem("t2", "more", target="drive:x"), FileContentItem("t3", ""))


@pytest.mark.parametrize("payload, expected", [
    ({"file_contents": "just text"}, (FileContentItem("t1", "just text"),)),
    ({"file_contents": {"name": "n.txt", "append_text": "x"}}, (FileContentItem("t1", "x", name="n.txt"),)),
    ({"file_contents": [{"file_ref": "drive:y", "text": "z"}]}, (FileContentItem("t1", "z", target="drive:y"),)),
    ({"text": "body", "name": "s.md"}, (FileContentItem("t1", "body", name="s.md"),)),
    ({"append_text": "tail", "file_ref": "drive:q"}, (FileContentItem("t1", "tail", target="drive:q"),)),
    ({"file_contents": [5, {"name": "no text"}, None]}, ()),
    ({"file_contents": 42}, ()),
    ({}, ()),
])
def test_accept_liberally(payload, expected):
    assert parse_file_contents(payload) == expected


def test_describe_shows_length_and_first_line_never_the_whole_text():
    item = FileContentItem("t1", "L" * 100 + "\nsecret second line", name="a.md")
    line = item.describe()
    assert line.startswith('t1 name "a.md" 119 characters, first line "') and "secret" not in line
    assert line.endswith("L" * 80 + '…"')
    assert FileContentItem("t2", "x", target="drive:z").describe() == 't2 target drive:z 1 characters, first line "x"'
    assert FileContentItem("t3", "").describe() == 't3 0 characters, first line ""'


def test_identical_searches_share_a_key_and_the_first_handle_is_kept():
    assert search_key({"query": "  Lease  Porto ", "kind": "PDF"}) == search_key({"query": "lease porto", "kind": "pdf"})
    assert search_key({"query": "lease"}) != search_key({"query": "lease", "folder": "Contracts"})
    c = PassThroughCollector()
    key = search_key({"query": "lease"})
    assert c.earlier_search(key) is None
    c.remember_search(key, "r1")
    c.remember_search(key, "r4")
    assert c.earlier_search(key) == "r1"


def test_drive_line_from_the_connection():
    assert DriveLine.of(DriveConnection(connected=True)) is DriveLine.CONNECTED
    assert DriveLine.of(DriveConnection(connected=True, needs_reconnect=True)) is DriveLine.RECONNECT_NEEDED
    assert DriveLine.of(DriveConnection(connected=False)) is DriveLine.NOT_CONNECTED


def _state(**kw):
    base = dict(user_id="u1", root_item_id="root", applied_versions={MIGRATION_GATE_KEY: "1"})
    return DriveSyncState(**{**base, **kw})


@pytest.mark.parametrize("enabled, state, connection, expected", [
    (False, None, DriveConnection(connected=True), SearchLine.OFF),
    (True, _state(), DriveConnection(connected=False), SearchLine.ON),
    (True, _state(drive_id="old"), DriveConnection(connected=True, drive_id="new"), SearchLine.REBUILDING),
    (True, _state(purging=True), DriveConnection(connected=True), SearchLine.REBUILDING),
    (True, None, DriveConnection(connected=True), SearchLine.BUILDING),
    (True, _state(applied_versions={}), DriveConnection(connected=True), SearchLine.REBUILDING),
    (True, _state(epoch_phase=EpochPhase.ENUMERATING), DriveConnection(connected=True), SearchLine.BUILDING),
    (True, _state(), DriveConnection(connected=True), SearchLine.ON),
])
def test_search_line_from_the_gate_facts(enabled, state, connection, expected):
    assert search_line(enabled, state, connection, NOW) is expected

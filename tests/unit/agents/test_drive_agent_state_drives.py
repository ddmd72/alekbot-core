"""The state block's `drives` lines (MULTI_DRIVE_RFC §11.2, slice 2a §4): one line per live connection after `search:`;
no mounts, no block."""
from datetime import datetime, timezone

from src.agents.drive_agent import render_state
from src.domain.drive_line import DriveLine
from src.domain.drive_mount_line import MountLine
from src.domain.drive_preflight import DrivePreflight
from src.domain.drive_search_line import SearchLine

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
HEADING = "  drives (a path without a drive name is on the primary drive):"
DRIVE = MountLine("Drive", True, DriveLine.CONNECTED, SearchLine.ON, True, True)
ACME = MountLine("Acme", False, DriveLine.RECONNECT_NEEDED, SearchLine.BUILDING, False, False)


def _state(*mounts: MountLine) -> list:
    preflight = DrivePreflight(DriveLine.CONNECTED, SearchLine.ON, mounts)
    return render_state({}, (), preflight, NOW, "UTC").split("\n")


def test_one_mount_prints_the_heading_and_its_line_after_search():
    lines = _state(DRIVE)
    assert lines[:5] == [
        "drive_state {", "  drive: connected", "  search: on", HEADING,
        "    - Drive: primary; connected; search on; writable; searched by default",
    ]
    assert lines[5].startswith("  now: ")


def test_two_mounts_print_in_order():
    lines = _state(DRIVE, ACME)
    assert lines[3:6] == [
        HEADING,
        "    - Drive: primary; connected; search on; writable; searched by default",
        "    - Acme: reconnect needed; search still building; read-only; searched only when named",
    ]


def test_no_mounts_no_drives_block():
    lines = _state()
    assert "drives" not in "\n".join(lines)
    assert lines[:3] == ["drive_state {", "  drive: connected", "  search: on"]
    assert lines[3].startswith("  now: ")

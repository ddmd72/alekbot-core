"""The connector's `/` lines (DRIVE_MCP_CONNECTOR_RFC §7.4, E10b; slice 3 DELTA 2): each visible drive shows its
connector access, never its write folder, scope or a writable / read-only part; the owner's note ends the line."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_connector_listing import (
    CONNECTOR_TOPS_HEAD, connector_mount_row, render_connector_mount_listing,
)
from src.domain.drive_mount import Mount
from src.domain.drive_mounts import MOUNT_LISTING_HEADING

T0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
DRIVE = DriveConnection(connection_id="conn-1", user_id="usr-alex", account_id="acc-alex", credential_id="cred-1",
                        kind="fake_a", drive_identity="fake_a:alex", ref_tag="a1", created_at=T0,
                        write_folder_id="f-rd", scope_folder_ids=("f-rd",))


def line(connection, *, primary=True, note="", paths=(), more=0) -> str:
    row = connector_mount_row(Mount(connection=connection, primary=primary), None, note=note, folder_paths=paths,
                              more=more)
    return render_connector_mount_listing([row]).split("\n")[1]


def test_whole_access_reads_everything_the_bot_sees():
    assert line(replace(DRIVE, connector_access=ConnectorAccess.whole_drive())) == (
        "- Drive: primary; connected; connector reads everything the bot sees on it; searched by default")


def test_folders_print_their_canonical_paths_and_the_rest_as_a_count():
    text = line(replace(DRIVE, connector_access=ConnectorAccess.folders(("f-rd", "f-oak", "f-x"))), primary=False,
                paths=("R&D", "12 Oak Street"), more=1)
    assert text == "- Drive: connected; connector reads only Drive/R&D, Drive/12 Oak Street +1 more; searched by default"
    assert "writable" not in text and "read-only" not in text and "indexes only" not in text


def test_folders_whose_paths_could_not_be_read_say_so():
    text = line(replace(DRIVE, connector_access=ConnectorAccess.folders(("f-rd",))))
    assert "connector reads chosen folders (their paths could not be read)" in text


def test_the_owner_note_ends_the_line_and_a_reconnect_shows():
    flagged = replace(DRIVE, connector_access=ConnectorAccess.whole_drive(), state=ConnectionState.NEEDS_RECONNECT,
                      searched_by_default=False)
    text = line(flagged, note='Alex Doe\'s "work" drive')
    assert text.endswith("; searched only when named; owner's note: \"Alex Doe's 'work' drive\"")
    assert "reconnect needed" in text


def test_the_listing_heading_and_the_tops_head():
    rows = [connector_mount_row(Mount(connection=replace(DRIVE, connector_access=ConnectorAccess.whole_drive()),
                                      primary=True), None, note="", folder_paths=(), more=0)]
    assert render_connector_mount_listing(rows).split("\n")[0] == MOUNT_LISTING_HEADING
    assert CONNECTOR_TOPS_HEAD.format(drive="Drive") == "Folders shared with this connector on Drive:"

"""G11: a mount line names the chosen folders (`indexes only <p1>, <p2>, ... +N more`) right after the writable part;
a line without a scope renders exactly as before. Invented cast only."""
from datetime import datetime, timezone

from src.domain.drive_connection import DriveConnection
from src.domain.drive_mounts import mount_line, mount_table, render_mount_line
from src.domain.drive_search_line import SearchLine

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def acme(**over):
    connection = DriveConnection(connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2",
                                 kind="fake_a", drive_identity="fake_a:conn-2", ref_tag="b2", mount_name="Acme",
                                 created_at=T0, **over)
    return mount_table([connection], "conn-2", NOW).mounts[0]


def test_one_path_prints_canonical_after_the_writable_part():
    line = mount_line(acme(), SearchLine.ON, scope_paths=["Acme Hamburg/Q3"])
    assert line.scope_folders == ("Acme/Acme Hamburg/Q3",) and line.scope_more == 0
    assert render_mount_line(line) == ("- Acme: primary; connected; search on; writable; "
                                       "indexes only Acme/Acme Hamburg/Q3; searched by default")


def test_five_paths_print_without_a_more_part():
    paths = [f"Clients: {2020 + n}" for n in range(5)]
    text = render_mount_line(mount_line(acme(), None, scope_paths=paths))
    assert "indexes only " + ", ".join(f"Acme/{p}" for p in paths) + ";" in text and "more" not in text


def test_seven_chosen_print_five_and_two_more():
    paths = [f"Clients: {2020 + n}" for n in range(5)]
    text = render_mount_line(mount_line(acme(), None, scope_paths=paths, scope_more=2))
    assert "indexes only " + ", ".join(f"Acme/{p}" for p in paths) + " +2 more; searched by default" in text


def test_the_part_follows_a_write_folder_part():
    text = render_mount_line(mount_line(acme(write_folder_id="fld-1"), None, write_folder_path="R&D", scope_paths=["R&D"]))
    assert text.index("writable only in Acme/R&D") < text.index("indexes only Acme/R&D")


def test_a_line_without_a_scope_renders_exactly_as_before():
    plain = mount_line(acme(), SearchLine.ON)
    assert plain.scope_folders == () and plain.scope_more == 0
    assert render_mount_line(plain) == "- Acme: primary; connected; search on; writable; searched by default"
    assert render_mount_line(mount_line(acme(), SearchLine.ON, scope_paths=(), scope_more=0)) == render_mount_line(plain)

"""Slice 2b in `drive_mounts` (MULTI_DRIVE_RFC §11.1, §11.2, §11.8, §11.10; design §1.7, E-6, E-7): `resolve_drives`
reads every drive searched by default and several names, `resolve_path` falls back to the one live mount, the mount
line prints the write folder and the owner's drive note, `drive_note_line` bounds the note, `renamed_history`."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_connection import ROOT_FOLDER, DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_line import DriveLine
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import (
    HOLD, NOTE_LINE_MAX, RENAME_NOTICE, drive_note_line, mount_line, mount_table, render_mount_line, renamed_history,
    resolve_drives, resolve_path, unknown_drive_line,
)
from src.domain.drive_search_line import SearchLine
from src.domain.drive_selection import DriveSelection
from src.domain.user_drive import DriveNotConnectedError

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
NOW = T1 + timedelta(days=1)


def conn(connection_id, name, tag, *, minutes=0, history=(), searched=True, write=ROOT_FOLDER,
         state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name,
                           created_at=T0 + timedelta(minutes=minutes), state=state, name_history=tuple(history),
                           searched_by_default=searched, write_folder_id=write)


def hamburg(**over):
    """`Acme Hamburg`, formerly `Acme` (renamed at T1)."""
    return conn("conn-3", "Acme Hamburg", "c3", minutes=2,
                history=(MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1)), **over)


def chosen(selection):
    assert isinstance(selection, DriveSelection), selection
    return [m.name for m in selection.mounts], selection.not_searched, selection.notice


class TestResolveDrives:
    @pytest.mark.parametrize("value", [None, "", " , "])
    def test_empty_reads_every_drive_searched_by_default(self, value):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Drive 2", "b2", minutes=1,
                                                                  searched=False), hamburg()], "conn-1", NOW)
        assert chosen(resolve_drives(table, value)) == (["Drive", "Acme Hamburg"], ("Drive 2",), "")

    def test_a_drive_needing_a_reconnect_is_read_when_flagged(self):
        table = mount_table([conn("conn-1", "Drive", "a1"),
                             conn("conn-2", "Drive 2", "b2", minutes=1, state=ConnectionState.NEEDS_RECONNECT)],
                            "conn-1", NOW)
        assert chosen(resolve_drives(table, "")) == (["Drive", "Drive 2"], (), "")

    def test_none_flagged_reads_the_primary(self):
        table = mount_table([conn("conn-1", "Drive", "a1", searched=False),
                             conn("conn-2", "Acme", "b2", minutes=1, searched=False)], "conn-2", NOW)
        assert chosen(resolve_drives(table, "")) == (["Acme"], ("Drive",), "")

    def test_none_flagged_and_no_primary_reads_the_oldest(self):
        table = mount_table([conn("conn-1", "Drive", "a1", searched=False),
                             conn("conn-2", "Acme", "b2", minutes=1, searched=False)], None, NOW)
        assert chosen(resolve_drives(table, "")) == (["Drive"], ("Acme",), "")

    def test_no_mount_reads_nothing(self):
        assert chosen(resolve_drives(mount_table([], None, NOW), "")) == ([], (), "")

    def test_two_names_read_both_in_table_order(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Drive 2", "b2", minutes=1), hamburg()],
                            "conn-1", NOW)
        assert chosen(resolve_drives(table, "acme hamburg, Drive, DRIVE")) == (
            ["Drive", "Acme Hamburg"], ("Drive 2",), "")

    def test_former_names_add_their_notices(self):
        renamed = conn("conn-1", "Drive 2", "a1",
                       history=(MountNameChange("Drive", T0), MountNameChange("Drive 2", T1)))
        table = mount_table([renamed, hamburg()], "conn-1", NOW)
        assert chosen(resolve_drives(table, "Acme, Drive")) == (
            ["Drive 2", "Acme Hamburg"], (),
            RENAME_NOTICE.format(former="Acme", current="Acme Hamburg") + "\n"
            + RENAME_NOTICE.format(former="Drive", current="Drive 2"))

    def test_a_current_and_a_former_name_of_one_drive_read_it_once(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), hamburg()], "conn-1", NOW)
        selection = resolve_drives(table, "Acme, Acme Hamburg")
        assert chosen(selection)[0] == ["Acme Hamburg"]

    def test_an_unknown_name_is_refused_with_the_list(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), hamburg()], "conn-1", NOW)
        assert resolve_drives(table, "Drive, Springfield") == unknown_drive_line("Springfield", table)
        assert resolve_drives(table, "Springfield") == \
            'There is no drive named "Springfield". The drives are: Drive, Acme Hamburg.'

    def test_a_former_name_after_the_hold_is_unknown(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), hamburg()], "conn-1", T1 + HOLD)
        assert isinstance(resolve_drives(table, "Acme"), str)


class TestResolvePath:
    def test_one_mount_and_no_primary_resolves_an_unprefixed_path_there(self):
        table = mount_table([conn("conn-2", "Acme", "b2", write=None)], None, NOW)
        target = resolve_path(table, "R&D/Tom's plan.docx")
        assert (target.mount.connection_id, target.path, target.named) == ("conn-2", "R&D/Tom's plan.docx", False)

    def test_two_mounts_and_no_primary_still_refuse(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b2", minutes=1)], None, NOW)
        with pytest.raises(DriveNotConnectedError):
            resolve_path(table, "R&D")
        assert resolve_path(table, "Acme/R&D").mount.connection_id == "conn-2"

    def test_no_mount_refuses(self):
        with pytest.raises(DriveNotConnectedError):
            resolve_path(mount_table([], None, NOW), "R&D")


class TestMountLine:
    def test_the_one_drive_app_folder_line_is_todays(self):
        table = mount_table([conn("conn-1", "Drive", "a1")], "conn-1", NOW)
        line = mount_line(table.mounts[0], SearchLine.ON)
        assert render_mount_line(line) == "- Drive: primary; connected; search on; writable; searched by default"

    def test_a_read_only_drive_searched_only_when_named(self):
        table = mount_table([conn("conn-2", "Acme", "b2", write=None, searched=False)], None, NOW)
        line = mount_line(table.mounts[0], None)
        assert (line.writable, line.searched_by_default, line.drive) == (False, False, DriveLine.CONNECTED)
        assert render_mount_line(line) == "- Acme: connected; read-only; searched only when named"

    def test_a_write_folder_below_the_root_prints_its_canonical_path(self):
        table = mount_table([conn("conn-2", "Acme", "b2", write="f-rd")], None, NOW)
        line = mount_line(table.mounts[0], None, write_folder_path="R&D")
        assert line.write_folder == "Acme/R&D"
        assert render_mount_line(line) == "- Acme: connected; writable only in Acme/R&D; searched by default"

    def test_a_write_folder_whose_path_was_not_read_says_writes_go_to_a_chosen_folder(self):
        # tool-shape review N2: plain "writable" read as "write anywhere"; the line names the chosen folder instead
        table = mount_table([conn("conn-2", "Acme", "b2", write="f-rd")], None, NOW)
        assert render_mount_line(mount_line(table.mounts[0], None)) == \
            "- Acme: connected; writes go to a chosen folder (its path could not be read); searched by default"

    def test_the_root_write_folder_ignores_a_path(self):
        table = mount_table([conn("conn-1", "Drive", "a1")], "conn-1", NOW)
        assert mount_line(table.mounts[0], None, write_folder_path="R&D").write_folder == ""

    def test_the_owners_note_ends_the_line(self):
        table = mount_table([conn("conn-2", "Acme", "b2", write=None)], None, NOW)
        line = mount_line(table.mounts[0], None, note='Acme work files:\n  R&D, "Tom\'s plan"')
        assert line.note == "Acme work files: R&D, 'Tom's plan'"
        assert render_mount_line(line) == ("- Acme: connected; read-only; searched by default; owner's note: "
                                           "\"Acme work files: R&D, 'Tom's plan'\"")


class TestDriveNoteLine:
    def test_bounded_at_the_max(self):
        exact = "a" * NOTE_LINE_MAX
        assert drive_note_line(exact) == exact
        assert drive_note_line(exact + "b") == exact + "…"

    def test_whitespace_and_quotes(self):
        assert drive_note_line('  Family documents:\t"Jamie\'s birth certificate"\n') == \
            "Family documents: 'Jamie's birth certificate'"


class TestRenamedHistory:
    def test_appends_the_new_name(self):
        renamed = renamed_history(hamburg(), "Acme", NOW)
        assert [c.name for c in renamed] == ["Acme", "Acme Hamburg", "Acme"] and renamed[-1].at == NOW

    def test_a_connection_stored_without_history_holds_its_old_name(self):
        renamed = renamed_history(conn("conn-2", "Acme", "b2"), "Acme Hamburg", NOW)
        assert renamed == (MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", NOW))


class TestWriteFolderPathUnreadable:
    def test_a_sub_folder_write_folder_with_no_path_does_not_print_plain_writable(self):
        table = mount_table([conn("conn-2", "Acme", "b2", write="f-rd")], None, NOW)
        text = render_mount_line(mount_line(table.mounts[0], None))
        assert "; writable;" not in text and "chosen folder" in text and "could not be read" in text

    def test_the_root_write_folder_still_prints_writable(self):
        table = mount_table([conn("conn-1", "Drive", "a1")], "conn-1", NOW)
        assert "; writable;" in render_mount_line(mount_line(table.mounts[0], None))

"""The `drives` value, canonical paths and the mount lines (MULTI_DRIVE_RFC §11.1, §11.2; slice 2a core):
`resolve_drives` (empty, one, a former name, two names, unknown; the exact refusals; `not_searched`), `canonical_path`,
`mount_table`, `mount_line`, `render_mount_line` / `render_mount_listing` (exact text), the fixed texts, and T-1 over
the core's new modules (no provider word in any string constant). Technical ids only; names from the invented
universe."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.domain import drive_mounts
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_line import DriveLine
from src.domain.drive_mount_line import MountLine
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import (
    canonical_path, mount_line, mount_table, render_mount_line, render_mount_listing, resolve_drives,
    unknown_drive_line,
)
from src.domain.drive_search_line import SearchLine
from src.domain.drive_selection import DriveSelection

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
NEEDS = ConnectionState.NEEDS_RECONNECT


def conn(connection_id, name, tag, *, minutes=0, history=(), state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name,
                           created_at=T0 + timedelta(minutes=minutes), state=state,
                           disconnected_at=T1 if state is ConnectionState.DISCONNECTED else None,
                           name_history=tuple(history))


def three(now=T1 + timedelta(days=1)):
    """`Drive` (primary), `Drive 2`, and `Acme Hamburg` (formerly `Acme`, renamed at T1)."""
    hamburg = conn("conn-3", "Acme Hamburg", "c3", minutes=2,
                   history=(MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1)))
    return mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Drive 2", "b7", minutes=1), hamburg],
                       "conn-1", now)


def chosen(selection):
    assert isinstance(selection, DriveSelection), selection
    return [m.name for m in selection.mounts], selection.not_searched, selection.notice


class TestResolveDrives:
    @pytest.mark.parametrize("value", [None, "", "  ", ",", " , "])
    def test_empty_is_the_primary_and_the_others_are_not_searched(self, value):
        assert chosen(resolve_drives(three(), value)) == (["Drive"], ("Drive 2", "Acme Hamburg"), "")

    def test_empty_with_one_drive_has_nothing_not_searched(self):
        table = mount_table([conn("conn-1", "Drive", "a1")], "conn-1", T1)
        assert chosen(resolve_drives(table, "")) == (["Drive"], (), "")

    def test_empty_without_a_primary_falls_to_the_oldest_live_mount(self):
        table = mount_table([conn("conn-1", "Drive", "a1", state=NEEDS), conn("conn-2", "Acme", "b7", minutes=1)],
                            None, T1)
        assert chosen(resolve_drives(table, "")) == (["Drive"], ("Acme",), "")

    def test_empty_without_any_mount_selects_nothing(self):
        assert chosen(resolve_drives(mount_table([], None, T1), "")) == ([], (), "")

    def test_one_name_selects_that_mount(self):
        assert chosen(resolve_drives(three(), "Drive 2")) == (["Drive 2"], ("Drive", "Acme Hamburg"), "")

    def test_one_name_compares_by_key(self):
        assert chosen(resolve_drives(three(), "  acme   hamburg ")) == (["Acme Hamburg"], ("Drive", "Drive 2"), "")

    def test_a_held_former_name_selects_with_the_notice(self):
        assert chosen(resolve_drives(three(), "acme")) == (
            ["Acme Hamburg"], ("Drive", "Drive 2"), "Acme is now called Acme Hamburg.")

    def test_an_expired_former_name_is_unknown(self):
        assert resolve_drives(three(now=T1 + timedelta(days=30)), "Acme") == (
            'There is no drive named "Acme". The drives are: Drive, Drive 2, Acme Hamburg.')

    def test_the_same_name_twice_is_one_name(self):
        assert chosen(resolve_drives(three(), "Drive 2, drive 2,")) == (["Drive 2"], ("Drive", "Acme Hamburg"), "")

    def test_two_names_are_refused(self):
        assert resolve_drives(three(), "Acme Hamburg, Drive") == (
            "Name one drive per search for now. The drives are: Drive, Drive 2, Acme Hamburg.")

    def test_two_names_are_refused_even_when_one_is_unknown(self):
        assert resolve_drives(three(), "Springfield, Drive") == drive_mounts.ONE_DRIVE_LINE.format(
            names="Drive, Drive 2, Acme Hamburg")

    def test_an_unknown_name_is_refused_with_the_list(self):
        assert resolve_drives(three(), " Springfield ") == (
            'There is no drive named "Springfield". The drives are: Drive, Drive 2, Acme Hamburg.')
        assert unknown_drive_line("Springfield", three()) == resolve_drives(three(), "Springfield")

    def test_a_disconnected_drive_is_unknown_and_never_listed(self):
        table = mount_table([conn("conn-1", "Drive", "a1"),
                             conn("conn-2", "Acme", "b7", minutes=1, state=ConnectionState.DISCONNECTED)],
                            "conn-1", T1)
        assert resolve_drives(table, "Acme") == 'There is no drive named "Acme". The drives are: Drive.'
        assert chosen(resolve_drives(table, "")) == (["Drive"], (), "")


class TestCanonicalPath:
    @pytest.mark.parametrize("mount,path,expected", [
        ("Drive", "", "Drive"),
        ("Drive", "/", "Drive"),
        ("Drive", "R&D/Tom's plan.docx", "Drive/R&D/Tom's plan.docx"),
        ("Drive", "/R&D/", "Drive/R&D"),
        ("Acme Hamburg", "birth-certificate.pdf", "Acme Hamburg/birth-certificate.pdf"),
    ])
    def test_forms(self, mount, path, expected):
        assert canonical_path(mount, path) == expected


class TestMountTable:
    def test_live_connections_in_order_with_the_primary(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b7", minutes=1, state=NEEDS),
                             conn("conn-3", "Drive 2", "c3", minutes=2, state=ConnectionState.DISCONNECTED)],
                            "conn-2", T1)
        assert table.names() == ("Drive", "Acme")
        assert [(m.name, m.primary, m.ref_tag, m.connection_id) for m in table.mounts] == [
            ("Drive", False, "a1", "conn-1"), ("Acme", True, "b7", "conn-2")]
        assert table.primary().name == "Acme"
        assert table.by_connection("conn-3") is None

    def test_a_pointer_at_a_disconnected_connection_is_no_primary(self):
        table = mount_table([conn("conn-1", "Drive", "a1", state=ConnectionState.DISCONNECTED)], "conn-1", T1)
        assert (table.mounts, table.primary()) == ((), None)

    def test_former_names_are_carried(self):
        table = three()
        assert table.by_connection("conn-3").former_names == ("Acme",)
        assert table.by_name("ACME") == (table.by_connection("conn-3"), True)
        assert table.by_name("Acme Hamburg") == (table.by_connection("conn-3"), False)
        assert table.by_name("") is None


class TestMountLines:
    def test_the_primary_line_in_the_preflight(self):
        table = three()
        line = mount_line(table.primary(), SearchLine.ON)
        assert line == MountLine(name="Drive", primary=True, drive=DriveLine.CONNECTED, search=SearchLine.ON,
                                 writable=True, searched_by_default=True)
        assert render_mount_line(line) == "- Drive: primary; connected; search on; writable; searched by default"

    def test_a_second_drive_needing_a_reconnect(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b7", minutes=1, state=NEEDS)],
                            "conn-1", T1)
        line = mount_line(table.by_connection("conn-2"), SearchLine.BUILDING)
        assert (line.drive, line.writable, line.searched_by_default) == (DriveLine.RECONNECT_NEEDED, False, False)
        assert render_mount_line(line) == (
            "- Acme: reconnect needed; search still building; read-only; searched only when named")

    def test_the_listing_line_has_no_search_part(self):
        line = mount_line(three().primary(), None)
        assert render_mount_line(line) == "- Drive: primary; connected; writable; searched by default"

    def test_the_listing(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b7", minutes=1)], "conn-1", T1)
        assert render_mount_listing([mount_line(m, None) for m in table.mounts]) == (
            "Drives (a path without a drive name is on the primary drive):\n"
            "- Drive: primary; connected; writable; searched by default\n"
            "- Acme: connected; read-only; searched only when named")


class TestTexts:
    def test_the_fixed_lines(self):
        assert drive_mounts.RENAME_NOTICE.format(former="Acme", current="Acme Hamburg") == (
            "Acme is now called Acme Hamburg.")
        assert drive_mounts.CROSS_DRIVE_LINE == "Moving or copying a file between drives is not available yet."
        assert drive_mounts.READ_ONLY_LINE == "Only the primary drive can be changed for now; this drive is read-only."
        assert drive_mounts.FOLDER_DRIVE_MISMATCH.format(folder="Acme/R&D", mount_a="Acme", mount_b="Drive") == (
            "The folder Acme/R&D is on Acme, not on Drive.")
        assert drive_mounts.TREE_ROOT == "/"


# ---------------------------------------------------------------------------------------------- T-1 over the core

ROOT = Path(__file__).resolve().parents[3]
CORE_MODULES = (
    "src/domain/drive_mounts.py", "src/domain/drive_mount.py", "src/domain/drive_mount_table.py",
    "src/domain/drive_mount_name_change.py", "src/domain/drive_path_target.py", "src/domain/drive_selection.py",
    "src/domain/drive_mount_name_check.py", "src/domain/drive_mount_line.py",
)


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_provider_word_in_the_cores_strings(module):
    from tests.unit.domain.test_multi_drive_no_provider_strings import _PROVIDER_RE
    source = (ROOT / module).read_text(encoding="utf-8")
    hits = [(n.lineno, n.value) for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _PROVIDER_RE.search(n.value)]
    assert hits == []

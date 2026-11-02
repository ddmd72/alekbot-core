"""`drive_mounts.resolve_path` and `is_tree_root` (MULTI_DRIVE_RFC §11.1, §11.8; slice 2a core): a written path becomes
(mount, connection-relative path); an unprefixed path is on the primary (or the given default); names compare by
`mount_name_key`; a mount shadows a primary top-level folder of the same name; a held former name resolves with the
rename notice until the hold ends. Technical ids only; names from the invented universe."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import is_tree_root, mount_table, resolve_path
from src.domain.user_drive import DriveNotConnectedError, DrivePathError

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PLAN = "R&D/Tom's plan.docx"


def conn(connection_id, name, tag, *, minutes=0, history=(), state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name,
                           created_at=T0 + timedelta(minutes=minutes), state=state,
                           disconnected_at=T1 if state is ConnectionState.DISCONNECTED else None,
                           name_history=tuple(history))


def two_drives(now=T1):
    """`Drive` (primary, conn-1) and `Acme` (conn-2)."""
    return mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b7", minutes=1)], "conn-1", now)


def renamed(now):
    """`Drive` (primary) and conn-2, called `Acme` at T0 and renamed `Acme Hamburg` at T1."""
    history = (MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1))
    return mount_table([conn("conn-1", "Drive", "a1"),
                        conn("conn-2", "Acme Hamburg", "b7", minutes=1, history=history)], "conn-1", now)


def where(target):
    return target.mount.name, target.path, target.named, target.notice


class TestUnprefixed:
    def test_an_unprefixed_path_is_on_the_primary(self):
        assert where(resolve_path(two_drives(), PLAN)) == ("Drive", PLAN, False, "")

    def test_an_empty_path_is_the_primary_root(self):
        assert where(resolve_path(two_drives(), "")) == ("Drive", "", False, "")
        assert where(resolve_path(two_drives(), None)) == ("Drive", "", False, "")

    def test_the_default_wins_over_the_primary_for_an_unprefixed_path(self):
        table = two_drives()
        acme = table.by_connection("conn-2")
        assert where(resolve_path(table, "R&D", default=acme)) == ("Acme", "R&D", False, "")

    def test_a_prefixed_path_ignores_the_default(self):
        table = two_drives()
        acme = table.by_connection("conn-2")
        assert where(resolve_path(table, "Drive/R&D", default=acme)) == ("Drive", "R&D", True, "")

    def test_segments_are_trimmed_and_rejoined(self):
        assert resolve_path(two_drives(), " /R&D/ Tom's plan.docx /").path == PLAN


class TestPrefixed:
    def test_a_mount_prefix_selects_the_mount(self):
        assert where(resolve_path(two_drives(), "Acme/R&D")) == ("Acme", "R&D", True, "")
        assert where(resolve_path(two_drives(), f"Drive/{PLAN}")) == ("Drive", PLAN, True, "")

    def test_the_mount_name_compares_case_insensitively(self):
        assert where(resolve_path(two_drives(), "acme/R&D")) == ("Acme", "R&D", True, "")

    def test_inner_whitespace_and_unicode_form_do_not_matter(self):
        table = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme Hamburg", "b7", minutes=1)],
                            "conn-1", T1)
        assert where(resolve_path(table, "Acme  Hamburg/x")) == ("Acme Hamburg", "x", True, "")
        nfd = mount_table([conn("conn-1", "Drive", "a1"), conn("conn-2", "Acmé", "b7", minutes=1)], "conn-1", T1)
        assert resolve_path(nfd, "Acme\u0301/R&D").mount.name == "Acmé"

    @pytest.mark.parametrize("path", ["Acme", "Acme/", "/Acme"])
    def test_the_mount_itself_is_its_root(self, path):
        assert where(resolve_path(two_drives(), path)) == ("Acme", "", True, "")


class TestShadowing:
    def test_a_mount_wins_over_a_primary_top_folder_of_the_same_name(self):
        # the primary `Drive` holds a top-level folder `Acme`; a mount is also called `Acme`
        table = two_drives()
        assert where(resolve_path(table, "Acme/R&D")) == ("Acme", "R&D", True, "")
        assert where(resolve_path(table, "Drive/Acme/R&D")) == ("Drive", "Acme/R&D", True, "")


class TestFormerName:
    def test_a_held_former_name_resolves_with_the_notice(self):
        target = resolve_path(renamed(T1 + timedelta(days=29)), "Acme/R&D")
        assert where(target) == ("Acme Hamburg", "R&D", True, "Acme is now called Acme Hamburg.")
        assert target.mount.connection_id == "conn-2"

    def test_the_hold_ends_after_thirty_days_and_the_path_falls_to_the_primary(self):
        assert where(resolve_path(renamed(T1 + timedelta(days=30)), "Acme/R&D")) == ("Drive", "Acme/R&D", False, "")

    def test_the_current_name_carries_no_notice(self):
        assert where(resolve_path(renamed(T1), "Acme Hamburg/R&D")) == ("Acme Hamburg", "R&D", True, "")


class TestRefusals:
    @pytest.mark.parametrize("path", ["R&D/../x", "..", "./R&D", "R&D\\x"])
    def test_dot_segments_and_backslashes_are_refused(self, path):
        with pytest.raises(DrivePathError):
            resolve_path(two_drives(), path)

    def test_no_primary_and_no_default_is_not_connected(self):
        # MULTI_DRIVE_RFC E64: one live mount with no primary takes an unprefixed path (test_drive_mounts_2b.py);
        # with two live mounts and no primary nothing tells which one, so the refusal stands
        table = mount_table([conn("conn-2", "Acme", "b7"), conn("conn-3", "Acme Hamburg", "c4", minutes=1)], None, T1)
        with pytest.raises(DriveNotConnectedError):
            resolve_path(table, "R&D")

    def test_a_prefixed_path_resolves_without_a_primary(self):
        table = mount_table([conn("conn-2", "Acme", "b7")], None, T1)
        assert where(resolve_path(table, "Acme/R&D")) == ("Acme", "R&D", True, "")

    def test_an_empty_table_is_not_connected(self):
        with pytest.raises(DriveNotConnectedError):
            resolve_path(mount_table([], None, T1), "")

    def test_a_disconnected_connection_is_no_mount(self):
        table = mount_table([conn("conn-1", "Drive", "a1"),
                             conn("conn-2", "Acme", "b7", minutes=1, state=ConnectionState.DISCONNECTED)],
                            "conn-1", T1)
        assert where(resolve_path(table, "Acme/R&D")) == ("Drive", "Acme/R&D", False, "")


class TestTreeRoot:
    @pytest.mark.parametrize("path,expected", [("/", True), (" / ", True), ("", False), (None, False),
                                               ("Drive", False), ("//", False), ("Drive/", False)])
    def test_only_a_slash_is_the_tree_root(self, path, expected):
        assert is_tree_root(path) is expected

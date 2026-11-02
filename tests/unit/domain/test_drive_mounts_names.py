"""Mount names (MULTI_DRIVE_RFC §11.8, E11; slice 2a core): `validate_mount_name` (every rule, its message, the order,
the normalised name, the shadowing warning), `default_mount_name` (live current and held former names taken,
DISCONNECTED names free) and `held_former_names` (newest first, de-duplicated, the strict 30-day boundary). Technical
ids only; names from the invented universe."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import (
    FORBIDDEN_NAME_CHARS, HOLD, MAX_NAME_CHARS, NAME_DOT, NAME_EMPTY, NAME_FORBIDDEN_CHARS, NAME_HELD, NAME_RESERVED,
    NAME_TAKEN, NAME_TOO_LONG, RESERVED_NAMES, SHADOW_WARNING, default_mount_name, held_former_names, mount_name_key,
    validate_mount_name,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
GONE = ConnectionState.DISCONNECTED


def conn(connection_id, name, tag, *, history=(), state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name, created_at=T0,
                           state=state, disconnected_at=T1 if state is GONE else None, name_history=tuple(history))


def renamed_acme():
    """conn-2: `Acme` from T0, `Acme Hamburg` from T1."""
    return conn("conn-2", "Acme Hamburg", "b7",
                history=(MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1)))


def check(raw, *, connection_id=None, connections=(), primary="conn-1", folders=(), now=T1):
    return validate_mount_name(raw, connection_id=connection_id, connections=list(connections),
                               primary_connection_id=primary, primary_top_folders=list(folders), now=now)


DRIVE = conn("conn-1", "Drive", "a1")


class TestValidateRules:
    def test_a_valid_name_is_normalised(self):
        result = check("  Acme   Hamburg ", connections=[DRIVE])
        assert (result.ok, result.name, result.error, result.warning) == (True, "Acme Hamburg", "", "")

    def test_the_name_is_nfc(self):
        assert check("Acme\u0301").name == "Acmé"

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty(self, raw):
        result = check(raw)
        assert (result.ok, result.error) == (False, NAME_EMPTY)
        assert NAME_EMPTY == "A drive name is required."

    def test_forty_characters_pass_and_forty_one_do_not(self):
        assert check("A" * MAX_NAME_CHARS).ok
        assert check("A" * (MAX_NAME_CHARS + 1)).error == NAME_TOO_LONG
        assert NAME_TOO_LONG == "A drive name can have at most 40 characters."

    def test_the_length_counts_the_normalised_name(self):
        assert check(" " + "A" * MAX_NAME_CHARS + "  ").ok

    @pytest.mark.parametrize("ch", list(FORBIDDEN_NAME_CHARS))
    def test_each_forbidden_character(self, ch):
        assert check(f"Acme{ch}Hamburg").error == NAME_FORBIDDEN_CHARS

    @pytest.mark.parametrize("ch", ["\x07", "\x00", "​"])
    def test_a_control_or_format_character(self, ch):
        assert check(f"Acme{ch}Hamburg").error == NAME_FORBIDDEN_CHARS

    def test_the_forbidden_message(self):
        assert NAME_FORBIDDEN_CHARS == 'A drive name cannot contain / \\ : * ? " < > | or control characters.'

    @pytest.mark.parametrize("raw", [".Acme", "Acme.", ".", "..", "..."])
    def test_a_leading_or_trailing_dot(self, raw):
        assert check(raw).error == NAME_DOT
        assert NAME_DOT == "A drive name cannot start or end with a dot."

    def test_an_inner_dot_is_fine(self):
        assert check("Acme.Hamburg").ok

    @pytest.mark.parametrize("reserved", RESERVED_NAMES)
    def test_each_reserved_name_in_another_case(self, reserved):
        raw = reserved.swapcase()
        result = check(raw)
        assert result.error == NAME_RESERVED.format(name=raw)
        assert result.error == f"{raw} is the name of a folder the bot manages; choose another name."

    def test_the_reserved_set(self):
        assert RESERVED_NAMES == ("_Transcripts", "Archive", "Long paths", "Inbox")

    def test_taken_by_another_live_connection(self):
        result = check("drive", connections=[DRIVE], connection_id="conn-2")
        assert result.error == NAME_TAKEN.format(name="drive") == "Another drive is already called drive."

    def test_taken_by_a_live_connection_when_naming_a_new_one(self):
        assert check("Drive", connections=[DRIVE], connection_id=None).error == "Another drive is already called Drive."

    def test_a_connection_keeps_its_own_current_name(self):
        assert check("Drive", connections=[DRIVE], connection_id="conn-1").ok

    def test_needs_reconnect_is_live(self):
        flagged = conn("conn-2", "Acme", "b7", state=ConnectionState.NEEDS_RECONNECT)
        assert check("Acme", connections=[DRIVE, flagged]).error == NAME_TAKEN.format(name="Acme")

    def test_free_when_the_other_is_disconnected(self):
        gone = conn("conn-2", "Acme", "b7", state=GONE)
        assert check("Acme", connections=[DRIVE, gone], connection_id="conn-3").ok

    def test_held_by_another_connections_former_name(self):
        result = check("Acme", connections=[DRIVE, renamed_acme()], connection_id="conn-3", now=T1 + timedelta(days=29))
        assert result.error == NAME_HELD.format(name="Acme")
        assert result.error == ("Acme was another drive's name until recently; it stays reserved for 30 days after a "
                                "rename.")

    def test_free_once_the_hold_ended(self):
        assert check("Acme", connections=[DRIVE, renamed_acme()], connection_id="conn-3", now=T1 + HOLD).ok

    def test_the_connections_own_former_name_is_allowed_back(self):
        assert check("Acme", connections=[DRIVE, renamed_acme()], connection_id="conn-2",
                     now=T1 + timedelta(days=1)).ok

    def test_a_disconnected_connections_former_name_is_free(self):
        gone = conn("conn-2", "Acme Hamburg", "b7", state=GONE,
                    history=(MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1)))
        assert check("Acme", connections=[DRIVE, gone], connection_id="conn-3", now=T1 + timedelta(days=1)).ok

    def test_the_first_failure_wins(self):
        # too long and holding a forbidden character: the length rule comes first
        assert check("/" * (MAX_NAME_CHARS + 1)).error == NAME_TOO_LONG
        # reserved and taken: reserved comes first
        inbox = conn("conn-2", "Inbox", "b7")
        assert check("Inbox", connections=[DRIVE, inbox]).error == NAME_RESERVED.format(name="Inbox")


class TestShadowWarning:
    def test_a_name_equal_to_a_primary_top_folder_warns(self):
        result = check("springfield", connections=[DRIVE], connection_id="conn-2", folders=["Springfield", "R&D"])
        assert (result.ok, result.error) == (True, "")
        assert result.warning == SHADOW_WARNING.format(name="springfield", primary="Drive")
        assert result.warning == "The folder springfield in Drive will be reached as Drive/springfield."

    def test_renaming_the_primary_itself_names_it_by_its_new_name(self):
        result = check("Springfield", connections=[DRIVE], connection_id="conn-1", folders=["Springfield"])
        assert result.warning == "The folder Springfield in Springfield will be reached as Springfield/Springfield."

    def test_no_warning_without_a_matching_folder(self):
        assert check("Acme", connections=[DRIVE], connection_id="conn-2", folders=["Springfield"]).warning == ""

    def test_no_warning_on_an_invalid_name(self):
        result = check("Inbox", connections=[DRIVE], folders=["Inbox"])
        assert (result.error, result.warning) == (NAME_RESERVED.format(name="Inbox"), "")


class TestDefaultMountName:
    def test_the_first_drive(self):
        assert default_mount_name([], T1) == "Drive"

    def test_drive_2_when_drive_is_live(self):
        assert default_mount_name([DRIVE], T1) == "Drive 2"

    def test_the_lowest_free_number(self):
        assert default_mount_name([DRIVE, conn("conn-3", "Drive 3", "c3")], T1) == "Drive 2"

    def test_the_comparison_is_by_key(self):
        assert default_mount_name([conn("conn-1", "drive", "a1")], T1) == "Drive 2"

    def test_a_held_former_name_is_skipped(self):
        # conn-2 was `Drive 2` until T1, then renamed `Acme`
        moved = conn("conn-2", "Acme", "b7", history=(MountNameChange("Drive 2", T0), MountNameChange("Acme", T1)))
        assert default_mount_name([DRIVE, moved], T1 + timedelta(days=29)) == "Drive 3"
        assert default_mount_name([DRIVE, moved], T1 + HOLD) == "Drive 2"

    def test_a_disconnected_connections_name_is_reused(self):
        assert default_mount_name([conn("conn-1", "Drive", "a1", state=GONE)], T1) == "Drive"


class TestHeldFormerNames:
    def history(self, *pairs):
        return tuple(MountNameChange(name, at) for name, at in pairs)

    def test_empty_and_single_entry_histories_hold_nothing(self):
        assert held_former_names((), "Drive", T1) == ()
        assert held_former_names(self.history(("Drive", T0)), "Drive", T1) == ()

    def test_newest_first(self):
        t2 = T1 + timedelta(days=5)
        h = self.history(("Drive 2", T0), ("Acme", T1), ("Acme Hamburg", t2))
        assert held_former_names(h, "Acme Hamburg", T1 + timedelta(days=10)) == ("Acme", "Drive 2")

    def test_the_boundary_is_strict(self):
        h = self.history(("Acme", T0), ("Acme Hamburg", T1))
        assert held_former_names(h, "Acme Hamburg", T1 + HOLD - timedelta(microseconds=1)) == ("Acme",)
        assert held_former_names(h, "Acme Hamburg", T1 + HOLD) == ()

    def test_a_name_left_twice_counts_once_by_its_newest_leaving(self):
        t2, t3 = T1 + timedelta(days=40), T1 + timedelta(days=50)
        h = self.history(("Acme", T0), ("Springfield", T1), ("Acme", t2), ("Acme Hamburg", t3))
        # `Acme` was left at T1 (expired) and again at t3 (held): held once, newest first before `Springfield` (left t2)
        assert held_former_names(h, "Acme Hamburg", t3 + timedelta(days=1)) == ("Acme", "Springfield")

    def test_the_current_name_is_never_a_former_name(self):
        t2 = T1 + timedelta(days=1)
        h = self.history(("Acme", T0), ("Springfield", T1), ("acme", t2))
        assert held_former_names(h, "acme", t2 + timedelta(days=1)) == ("Springfield",)

    def test_the_hold_is_thirty_days(self):
        assert HOLD == timedelta(days=30)


class TestNameKey:
    def test_nfc_trim_whitespace_and_case(self):
        assert mount_name_key("  ACME \t Hamburg ") == mount_name_key("acme hamburg") == "acme hamburg"
        assert mount_name_key("Acme\u0301") == mount_name_key("ACMÉ")

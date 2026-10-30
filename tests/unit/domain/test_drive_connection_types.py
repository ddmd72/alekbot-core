"""The multi-drive value types of slice 1b (MULTI_DRIVE_RFC §3, §4.4, §6, §7): scope, handle, connection and its
state rule, status, user settings, capabilities, content identity, media source and media facts, and the neutral
fields of DriveItem / DriveChange."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.connection_scope import ConnectionScope, check_connection_id
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import DEFAULT_MOUNT_NAME, DriveConnection, connection_doc_id
from src.domain.drive_connection_state import ConnectionState, can_transition
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_media_source import MediaSource
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveItem

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ACTIVE, NEEDS, GONE = ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT, ConnectionState.DISCONNECTED


def _connection(**over) -> DriveConnection:
    base = dict(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1", kind="fake_a",
                drive_identity="fake_a:drive-1", ref_tag="a1", created_at=NOW)
    base.update(over)
    return DriveConnection(**base)


class TestConnectionScope:
    def test_fields_and_frozen(self):
        scope = ConnectionScope(user_id="usr-1", connection_id="conn-1")
        assert (scope.user_id, scope.connection_id) == ("usr-1", "conn-1")
        with pytest.raises(FrozenInstanceError):
            scope.connection_id = "conn-2"   # type: ignore[misc]

    @pytest.mark.parametrize("user_id,connection_id", [
        ("", "conn-1"), ("usr-1", ""), ("usr-1", "conn:1"), ("usr-1", "conn/1"),
    ])
    def test_refused(self, user_id, connection_id):
        with pytest.raises(ValueError):
            ConnectionScope(user_id=user_id, connection_id=connection_id)

    def test_check_connection_id(self):
        check_connection_id("conn-1")
        with pytest.raises(ValueError):
            check_connection_id("a:b")


class TestDriveHandle:
    def test_scope(self):
        handle = DriveHandle(user_id="usr-1", connection_id="conn-1", credential_id="cred-1", kind="fake_a")
        assert handle.scope == ConnectionScope(user_id="usr-1", connection_id="conn-1")

    @pytest.mark.parametrize("field", ["user_id", "connection_id", "credential_id", "kind"])
    def test_every_field_is_required(self, field):
        values = dict(user_id="usr-1", connection_id="conn-1", credential_id="cred-1", kind="fake_a")
        values[field] = ""
        with pytest.raises(ValueError):
            DriveHandle(**values)

    def test_a_connection_id_with_a_colon_is_refused(self):
        with pytest.raises(ValueError):
            DriveHandle(user_id="usr-1", connection_id="c:1", credential_id="cred-1", kind="fake_a")

    def test_the_handle_carries_no_mount_name_tag_or_policy(self):
        assert set(DriveHandle.__dataclass_fields__) == {"user_id", "connection_id", "credential_id", "kind"}


class TestConnectionState:
    @pytest.mark.parametrize("current,to,allowed", [
        (ACTIVE, NEEDS, True), (NEEDS, ACTIVE, True), (ACTIVE, GONE, True), (NEEDS, GONE, True),
        (ACTIVE, ACTIVE, False), (NEEDS, NEEDS, False), (GONE, GONE, False), (GONE, ACTIVE, False),
        (GONE, NEEDS, False),
    ])
    def test_can_transition(self, current, to, allowed):
        assert can_transition(current, to) is allowed

    def test_stored_values(self):
        assert [s.value for s in ConnectionState] == ["active", "needs_reconnect", "disconnected"]


class TestDriveConnection:
    def test_defaults(self):
        conn = _connection()
        assert conn.mount_name == DEFAULT_MOUNT_NAME == "Drive"
        assert (conn.indexing_enabled, conn.transcribe_enabled) == (False, False)
        assert (conn.state, conn.disconnected_at) == (ACTIVE, None)

    def test_handle_scope_and_doc_id(self):
        conn = _connection()
        assert conn.handle == DriveHandle(user_id="usr-1", connection_id="conn-1", credential_id="cred-1",
                                          kind="fake_a")
        assert conn.scope == ConnectionScope(user_id="usr-1", connection_id="conn-1")
        assert conn.doc_id == connection_doc_id(conn.scope) == "usr-1:conn-1"

    @pytest.mark.parametrize("over", [
        {"connection_id": ""}, {"connection_id": "c:1"}, {"user_id": ""}, {"account_id": ""},
        {"credential_id": ""}, {"kind": ""}, {"drive_identity": ""}, {"mount_name": ""}, {"ref_tag": "A1"},
        {"ref_tag": "a"}, {"state": GONE}, {"disconnected_at": NOW},
    ])
    def test_refused(self, over):
        with pytest.raises(ValueError):
            _connection(**over)

    def test_a_disconnected_connection_carries_its_time(self):
        conn = _connection(state=GONE, disconnected_at=NOW)
        assert conn.disconnected_at == NOW

    def test_with_state_flag_and_clear(self):
        flagged = _connection().with_state(NEEDS, NOW)
        assert (flagged.state, flagged.disconnected_at) == (NEEDS, None)
        assert flagged.with_state(ACTIVE, NOW).state is ACTIVE

    def test_with_state_disconnect_stamps_the_time_and_is_terminal(self):
        later = NOW + timedelta(days=1)
        gone = _connection().with_state(GONE, later)
        assert (gone.state, gone.disconnected_at) == (GONE, later)
        for to in ConnectionState:
            with pytest.raises(ValueError):
                gone.with_state(to, later)

    def test_with_state_to_the_same_state_is_refused(self):
        with pytest.raises(ValueError):
            _connection().with_state(ACTIVE, NOW)

    def test_frozen(self):
        with pytest.raises(FrozenInstanceError):
            _connection().state = NEEDS   # type: ignore[misc]


class TestStatusAndSettings:
    @pytest.mark.parametrize("state,expected", [
        (ACTIVE, DriveConnectionStatus(connected=True, needs_reconnect=False)),
        (NEEDS, DriveConnectionStatus(connected=True, needs_reconnect=True)),
    ])
    def test_status_of_a_live_connection(self, state, expected):
        assert DriveConnectionStatus.of(_connection(state=state)) == expected

    def test_status_of_none_or_disconnected(self):
        assert DriveConnectionStatus.of(None) == DriveConnectionStatus(connected=False)
        gone = _connection(state=GONE, disconnected_at=NOW)
        assert DriveConnectionStatus.of(gone) == DriveConnectionStatus(connected=False, needs_reconnect=False)

    def test_status_has_no_drive_id(self):
        assert set(DriveConnectionStatus.__dataclass_fields__) == {"connected", "needs_reconnect"}

    def test_settings(self):
        assert DriveUserSettings(user_id="usr-1").primary_connection_id is None
        assert DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1").primary_connection_id == "conn-1"

    @pytest.mark.parametrize("user_id,primary", [("", None), ("usr-1", ""), ("usr-1", "c:1")])
    def test_settings_refused(self, user_id, primary):
        with pytest.raises(ValueError):
            DriveUserSettings(user_id=user_id, primary_connection_id=primary)


class TestCapabilitiesAndIdentities:
    def test_capabilities(self):
        assert DriveCapabilities(max_path_chars=400).max_path_chars == 400
        assert set(DriveCapabilities.__dataclass_fields__) == {"max_path_chars"}
        for bad in (0, -1):
            with pytest.raises(ValueError):
                DriveCapabilities(max_path_chars=bad)

    def test_content_identity_renders(self):
        assert ContentIdentity(family="fam1", digest="aGFzaA/x+==", size=1024).render() == "fam1:aGFzaA/x+==:1024"
        assert ContentIdentity("fam1", "d", 0).render() == "fam1:d:0"

    @pytest.mark.parametrize("family,digest,size", [
        ("", "d", 1), ("Fam", "d", 1), ("fa:m", "d", 1), ("fam", "", 1), ("fam", "d", -1),
    ])
    def test_content_identity_refused(self, family, digest, size):
        with pytest.raises(ValueError):
            ContentIdentity(family=family, digest=digest, size=size)

    def test_media_source_keeps_secrets_out_of_repr(self):
        source = MediaSource(url="https://files.example.com/s3cr3t-token", headers={"Authorization": "Bearer t0k3n"},
                             expires_at=NOW)
        text = repr(source)
        assert "s3cr3t" not in text and "t0k3n" not in text and "Authorization" not in text
        assert not source.header_free

    def test_a_pre_signed_media_source_is_header_free(self):
        source = MediaSource(url="https://files.example.com/presigned")
        assert source.header_free and source.expires_at is None

    def test_media_source_needs_a_url(self):
        with pytest.raises(ValueError):
            MediaSource(url="")


class TestMediaFacts:
    def test_defaults_say_nothing(self):
        facts = DriveMediaFacts()
        assert facts.location is None and not facts.is_alias and not facts.package and not facts.malware
        assert facts.taken_at is None and facts.camera_make == "" and facts.video_duration_ms == 0

    def test_location_and_alias(self):
        facts = DriveMediaFacts(latitude=1.5, longitude=-2.25, alias_of="fake_a:drive-9:item-9")
        assert facts.location == (1.5, -2.25)
        assert facts.is_alias
        assert DriveMediaFacts(alias_of="").is_alias   # an alias whose target the provider did not name

    @pytest.mark.parametrize("over", [
        {"latitude": 1.0}, {"longitude": 1.0}, {"taken_at": datetime(2026, 1, 1)}, {"image_width": -1},
        {"video_duration_ms": -5}, {"audio_duration_ms": -1},
    ])
    def test_refused(self, over):
        with pytest.raises(ValueError):
            DriveMediaFacts(**over)


class TestNeutralFieldsOnItemAndChange:
    def test_drive_item_defaults(self):
        item = DriveItem(item_id="item-1", name="report.pdf", path="report.pdf", is_folder=False)
        assert (item.identity, item.drive_identity, item.content_version) == ("", "", "")
        assert item.content_identity is None and item.media == DriveMediaFacts()

    def test_drive_item_carries_the_adapter_built_shapes(self):
        ci = ContentIdentity("fam1", "d1", 10)
        item = DriveItem(item_id="item-1", name="report.pdf", path="report.pdf", is_folder=False,
                         identity="fake_a:drive-1:item-1", drive_identity="fake_a:drive-1", content_version="v2",
                         content_identity=ci, media=DriveMediaFacts(image_width=4))
        assert replace(item, content_version="v3").content_identity == ci
        assert item.media.image_width == 4

    def test_drive_change_and_page_defaults(self):
        change = DriveChange(item_id="item-1")
        assert (change.identity, change.content_identity, change.media) == ("", None, DriveMediaFacts())
        page = DriveChanges(changes=(change,), cursor="c", complete=True, full_enumeration=False,
                            root_item_id="r")
        assert page.drive_identity == ""

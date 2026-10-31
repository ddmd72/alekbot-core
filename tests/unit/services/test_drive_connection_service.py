"""DriveConnectionService behaviour (MULTI_DRIVE_RFC §4.4, §9; slice 1b P1b-1): connect and reconnect by account, tags
and names, the primary pointer, the stale-ref lines, the per-connection switches, the reconnect flag kept in step with
the credential, disconnect with primary handover, and R1 reading connections. Over the in-memory connection repository
and the real credential store on the evaluating Firestore fake; assertions on what is stored and what readers return."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_handle import DriveHandle
from src.domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds

KIND = "microsoft_onedrive"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ACTIVE, NEEDS, GONE = ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT, ConnectionState.DISCONNECTED


class World:
    def __init__(self, tags=None, ids=None) -> None:
        self.repo = FakeDriveConnectionRepository()
        self.store = CredentialsStore()
        self.ticks = count()
        tag_source = iter(tags) if tags is not None else (f"t{n}" for n in count(1))
        id_source = iter(ids) if ids is not None else (f"conn-{n}" for n in count(1))
        self.service = DriveConnectionService(
            self.repo, self.store.repo,
            clock=lambda: T0 + timedelta(minutes=next(self.ticks)),
            new_id=lambda: next(id_source), new_tag=lambda: next(tag_source))

    async def consent(self, user_id="usr-1", subject="subject-a", **over):
        """Save a drive credential as the connect service does; returns it with its stored id."""
        credential = creds(user_id, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"),
                           **over)
        credential_id = await self.store.repo.save(credential)
        return replace(credential, credential_id=credential_id)

    async def connect(self, user_id="usr-1", subject="subject-a", identity="microsoft_onedrive:drive-a"):
        return await self.service.activate(await self.consent(user_id, subject), identity)

    def stored(self, user_id, connection_id):
        return self.repo.connections[(user_id, connection_id)]

    def primary_id(self, user_id="usr-1"):
        settings = self.repo.settings.get(user_id)
        return None if settings is None else settings.primary_connection_id


# ---------------------------------------------------------------------------------------------- activate

class TestActivateCreates:
    async def test_a_first_consent_creates_an_active_primary_connection(self):
        w = World()
        credential = await w.consent()
        conn = await w.service.activate(credential, "microsoft_onedrive:drive-a")
        stored = w.stored("usr-1", "conn-1")
        assert conn == stored
        assert (stored.user_id, stored.account_id, stored.credential_id, stored.kind) == (
            "usr-1", credential.account_id, credential.credential_id, KIND)
        assert (stored.drive_identity, stored.ref_tag, stored.mount_name) == ("microsoft_onedrive:drive-a", "t1", "Drive")
        assert (stored.state, stored.indexing_enabled, stored.transcribe_enabled) == (ACTIVE, False, False)
        assert stored.created_at == T0
        assert w.primary_id() == "conn-1"

    async def test_a_second_account_is_a_second_connection_named_drive_2_and_the_primary_stays(self):
        w = World()
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert (second.connection_id, second.ref_tag, second.mount_name) == ("conn-2", "t2", "Drive 2")
        assert w.primary_id() == "conn-1"

    async def test_a_tag_already_held_or_invalid_is_skipped(self):
        w = World(tags=["a1", "a1", "BAD", "9x", "b2"])
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert second.ref_tag == "b2"

    async def test_a_taken_connection_id_is_retried_with_a_new_id(self):
        w = World(ids=["conn-1", "conn-1", "conn-2"])
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert second.connection_id == "conn-2"
        assert w.stored("usr-1", "conn-1").drive_identity == "microsoft_onedrive:drive-a"

    async def test_no_free_tag_raises_and_stores_nothing(self):
        w = World(tags=["a1"] * 100)
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        with pytest.raises(RuntimeError):
            await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert len(w.repo.connections) == 1

    async def test_a_connect_after_a_disconnect_is_a_new_connection_and_the_old_tag_stays_reserved(self):
        w = World(tags=["a1", "a1", "b2"])
        first = await w.connect()
        assert await w.service.disconnect("usr-1", first.connection_id) is True
        again = await w.connect()
        assert (again.connection_id, again.ref_tag, again.mount_name) == ("conn-2", "b2", "Drive")
        assert w.stored("usr-1", "conn-1").state is GONE
        assert w.primary_id() == "conn-2"

    async def test_the_lowest_free_name_is_reused(self):
        w = World()
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await w.service.disconnect("usr-1", second.connection_id)
        third = await w.connect(subject="subject-c", identity="microsoft_onedrive:drive-c")
        assert third.mount_name == "Drive 2"

    async def test_users_are_separate(self):
        w = World()
        await w.connect(user_id="usr-1")
        other = await w.connect(user_id="usr-2")
        assert other.user_id == "usr-2" and other.mount_name == "Drive"
        assert w.primary_id("usr-2") == other.connection_id

    @pytest.mark.parametrize("over", [{"credential_id": ""}, {"account_id": ""}, {"account_id": None}])
    async def test_a_credential_without_its_ids_is_refused(self, over):
        w = World()
        credential = replace(await w.consent(), **over)
        with pytest.raises(ValueError):
            await w.service.activate(credential, "microsoft_onedrive:drive-a")
        assert w.repo.connections == {}

    async def test_an_empty_drive_identity_is_refused(self):
        w = World()
        with pytest.raises(ValueError):
            await w.service.activate(await w.consent(), "")
        assert w.repo.connections == {}

    async def test_two_concurrent_consents_of_one_account_make_one_connection(self):
        w = World()
        credential = await w.consent()
        first, second = await asyncio.gather(w.service.activate(credential, "microsoft_onedrive:drive-a"),
                                             w.service.activate(credential, "microsoft_onedrive:drive-a"))
        assert first.connection_id == second.connection_id
        assert len(w.repo.connections) == 1


class TestActivateReconnects:
    async def test_the_same_credential_reconnects_a_flagged_connection(self):
        w = World()
        conn = await w.connect()
        assert await w.service.flag_reconnect(conn.handle) is True
        again = await w.connect()
        assert (again.connection_id, again.ref_tag, again.state) == (conn.connection_id, conn.ref_tag, ACTIVE)
        assert len(w.repo.connections) == 1

    async def test_the_same_drive_with_a_new_credential_is_the_same_connection_pointed_at_the_new_grant(self):
        w = World()
        conn = await w.connect()
        await w.store.repo.delete("usr-1", conn.credential_id)         # the credential is gone, the connection ACTIVE
        fresh = await w.consent()
        assert fresh.credential_id != conn.credential_id
        again = await w.service.activate(fresh, conn.drive_identity)
        assert again.connection_id == conn.connection_id
        assert w.stored("usr-1", conn.connection_id).credential_id == fresh.credential_id

    async def test_a_reconnect_reading_another_drive_identity_stores_it(self):
        w = World()
        conn = await w.connect(identity="microsoft_onedrive:drive-a")
        again = await w.connect(identity="microsoft_onedrive:drive-z")
        assert again.connection_id == conn.connection_id
        assert w.stored("usr-1", conn.connection_id).drive_identity == "microsoft_onedrive:drive-z"

    async def test_a_reconnect_keeps_the_switches(self):
        w = World()
        conn = await w.connect()
        await w.service.set_indexing("usr-1", conn.connection_id, True)
        await w.service.set_transcribe("usr-1", conn.connection_id, True)
        again = await w.connect()
        assert (again.indexing_enabled, again.transcribe_enabled) == (True, True)

    async def test_a_reconnect_of_a_non_primary_does_not_move_the_primary(self):
        w = World()
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert w.primary_id() == "conn-1"
        assert second.connection_id == "conn-2"

    async def test_a_lost_pointer_is_set_by_the_next_consent(self):
        w = World()
        conn = await w.connect()
        w.repo.settings["usr-1"] = DriveUserSettings(user_id="usr-1", primary_connection_id="conn-missing")
        await w.connect()
        assert w.primary_id() == conn.connection_id


# ---------------------------------------------------------------------------------------------- reads

class TestHandlesAndPrimary:
    async def test_handle_for_a_live_connection(self):
        w = World()
        conn = await w.connect()
        assert await w.service.handle_for("usr-1", conn.connection_id) == DriveHandle(
            user_id="usr-1", connection_id=conn.connection_id, credential_id=conn.credential_id, kind=KIND)
        await w.service.flag_reconnect(conn.handle)
        assert (await w.service.handle_for("usr-1", conn.connection_id)).connection_id == conn.connection_id

    @pytest.mark.parametrize("user_id, connection_id", [("usr-1", "conn-x"), ("usr-2", "conn-1")])
    async def test_handle_for_an_absent_or_foreign_connection_raises(self, user_id, connection_id):
        w = World()
        await w.connect()
        with pytest.raises(DriveNotConnectedError):
            await w.service.handle_for(user_id, connection_id)

    async def test_handle_for_a_disconnected_connection_raises(self):
        w = World()
        conn = await w.connect()
        await w.service.disconnect("usr-1", conn.connection_id)
        with pytest.raises(DriveNotConnectedError):
            await w.service.handle_for("usr-1", conn.connection_id)

    async def test_primary_and_its_handle(self):
        w = World()
        assert await w.service.primary("usr-1") is None
        with pytest.raises(DriveNotConnectedError):
            await w.service.primary_handle("usr-1")
        conn = await w.connect()
        assert await w.service.primary("usr-1") == conn
        assert await w.service.primary_handle("usr-1") == conn.handle

    async def test_a_primary_that_needs_a_reconnect_is_no_primary(self):
        w = World()
        conn = await w.connect()
        await w.service.flag_reconnect(conn.handle)
        assert await w.service.primary("usr-1") is None
        with pytest.raises(DriveNotConnectedError):
            await w.service.primary_handle("usr-1")
        assert w.primary_id() == conn.connection_id          # the pointer stays; a reconnect brings it back

    async def test_set_primary_names_an_active_connection(self):
        w = World()
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await w.service.set_primary("usr-1", second.connection_id)
        assert w.primary_id() == second.connection_id
        assert (await w.service.primary("usr-1")).connection_id == second.connection_id

    async def test_set_primary_refuses_a_flagged_disconnected_absent_or_foreign_connection(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        flagged = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        gone = await w.connect(subject="subject-c", identity="microsoft_onedrive:drive-c")
        await w.service.flag_reconnect(flagged.handle)
        await w.service.disconnect("usr-1", gone.connection_id)
        for user_id, connection_id in (("usr-1", flagged.connection_id), ("usr-1", gone.connection_id),
                                       ("usr-1", "conn-x"), ("usr-2", first.connection_id)):
            with pytest.raises(DriveNotConnectedError):
                await w.service.set_primary(user_id, connection_id)
        assert w.primary_id() == first.connection_id

    async def test_list_for_user_hides_disconnected_unless_asked(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await w.service.disconnect("usr-1", first.connection_id)
        assert [c.connection_id for c in await w.service.list_for_user("usr-1")] == [second.connection_id]
        assert [c.connection_id for c in await w.service.list_for_user("usr-1", include_disconnected=True)] == [
            first.connection_id, second.connection_id]
        assert (await w.service.get("usr-1", first.connection_id)).state is GONE
        assert await w.service.get("usr-2", first.connection_id) is None


class TestStatus:
    async def test_no_connection_reads_not_connected(self):
        assert await World().service.status("usr-1") == DriveConnectionStatus(connected=False)

    async def test_an_active_primary_reads_connected(self):
        w = World()
        await w.connect()
        assert await w.service.status("usr-1") == DriveConnectionStatus(connected=True, needs_reconnect=False)

    async def test_a_flagged_primary_reads_connected_needs_reconnect(self):
        w = World()
        conn = await w.connect()
        await w.service.flag_reconnect(conn.handle)
        assert await w.service.status("usr-1") == DriveConnectionStatus(connected=True, needs_reconnect=True)

    async def test_a_disconnected_drive_reads_not_connected(self):
        w = World()
        conn = await w.connect()
        await w.service.disconnect("usr-1", conn.connection_id)
        assert await w.service.status("usr-1") == DriveConnectionStatus(connected=False)

    async def test_without_a_live_pointer_the_oldest_live_connection_answers(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await w.service.flag_reconnect(second.handle)
        await w.service.disconnect("usr-1", first.connection_id)   # the only ACTIVE other is none: pointer cleared
        assert w.primary_id() is None
        assert await w.service.status("usr-1") == DriveConnectionStatus(connected=True, needs_reconnect=True)


class TestResolveRef:
    async def test_a_tag_of_a_live_connection_resolves(self):
        w = World(tags=["a1"])
        conn = await w.connect()
        resolved = await w.service.resolve_ref("usr-1", "open [Drive: report.pdf ref=drive:a1:item-1]")
        assert resolved == ResolvedDriveRef(connection=conn, item_id="item-1")
        assert resolved.handle == conn.handle

    async def test_an_item_id_with_colons_resolves_whole(self):
        w = World(tags=["a1"])
        await w.connect()
        resolved = await w.service.resolve_ref("usr-1", "drive:a1:item:1:2")
        assert resolved.item_id == "item:1:2"

    async def test_a_flagged_connection_still_resolves(self):
        w = World(tags=["a1"])
        conn = await w.connect()
        await w.service.flag_reconnect(conn.handle)
        assert isinstance(await w.service.resolve_ref("usr-1", "drive:a1:item-1"), ResolvedDriveRef)

    async def test_an_untagged_ref_answers_search_again(self):
        w = World(tags=["a1"])
        await w.connect()
        assert await w.service.resolve_ref("usr-1", "see drive:01ABCDEF") == DriveRefRefusal(
            line=stale_ref_line("drive:01ABCDEF"))

    async def test_a_tag_the_user_never_had_answers_search_again(self):
        w = World(tags=["a1"])
        await w.connect(user_id="usr-2")
        assert await w.service.resolve_ref("usr-1", "drive:a1:item-1") == DriveRefRefusal(
            line=stale_ref_line("drive:a1:item-1"))

    async def test_a_disconnected_tag_answers_no_longer_connected(self):
        w = World(tags=["a1", "b2"])
        conn = await w.connect()
        await w.service.disconnect("usr-1", conn.connection_id)
        await w.connect()                                    # a newer connection of the same account: tag b2
        assert await w.service.resolve_ref("usr-1", "drive:a1:item-1") == DriveRefRefusal(
            line=disconnected_ref_line("drive:a1:item-1"))

    async def test_text_without_a_ref_is_none(self):
        assert await World().service.resolve_ref("usr-1", "no ref here") is None


class TestSwitches:
    async def test_set_indexing_and_transcribe_are_stored_per_connection(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        on = await w.service.set_indexing("usr-1", first.connection_id, True)
        assert on.indexing_enabled is True and on == w.stored("usr-1", first.connection_id)
        assert (await w.service.set_transcribe("usr-1", first.connection_id, True)).transcribe_enabled is True
        assert (w.stored("usr-1", second.connection_id).indexing_enabled,
                w.stored("usr-1", second.connection_id).transcribe_enabled) == (False, False)
        assert (await w.service.set_indexing("usr-1", first.connection_id, False)).indexing_enabled is False

    async def test_a_flagged_connection_can_be_switched(self):
        w = World()
        conn = await w.connect()
        await w.service.flag_reconnect(conn.handle)
        assert (await w.service.set_indexing("usr-1", conn.connection_id, True)).state is NEEDS

    @pytest.mark.parametrize("setter", ["set_indexing", "set_transcribe"])
    async def test_an_absent_or_disconnected_connection_is_refused(self, setter):
        w = World()
        conn = await w.connect()
        with pytest.raises(DriveNotConnectedError):
            await getattr(w.service, setter)("usr-1", "conn-x", True)
        await w.service.disconnect("usr-1", conn.connection_id)
        with pytest.raises(DriveNotConnectedError):
            await getattr(w.service, setter)("usr-1", conn.connection_id, True)
        assert w.stored("usr-1", conn.connection_id).indexing_enabled is False
        assert w.stored("usr-1", conn.connection_id).transcribe_enabled is False


# ---------------------------------------------------------------------------------------------- state

class TestFlagReconnect:
    async def test_the_first_flag_moves_the_state_and_the_credential(self):
        w = World()
        conn = await w.connect()
        assert await w.service.flag_reconnect(conn.handle) is True
        assert w.stored("usr-1", conn.connection_id).state is NEEDS
        assert (await w.store.repo.get("usr-1", conn.credential_id)).needs_reconnect is True
        assert await w.service.flag_reconnect(conn.handle) is False

    async def test_a_stale_handle_of_a_replaced_grant_flags_nothing(self):
        w = World()
        conn = await w.connect()
        stale = replace(conn.handle, credential_id="cred-old")
        assert await w.service.flag_reconnect(stale) is False
        assert w.stored("usr-1", conn.connection_id).state is ACTIVE

    async def test_absent_or_disconnected_flags_nothing(self):
        w = World()
        conn = await w.connect()
        assert await w.service.flag_reconnect(replace(conn.handle, connection_id="conn-x")) is False
        await w.service.disconnect("usr-1", conn.connection_id)
        assert await w.service.flag_reconnect(conn.handle) is False
        assert w.stored("usr-1", conn.connection_id).state is GONE

    async def test_a_credential_write_failure_keeps_the_transition(self, caplog):
        w = World()
        conn = await w.connect()

        async def broken(credential):
            raise RuntimeError("store down")
        w.store.repo.update = broken
        assert await w.service.flag_reconnect(conn.handle) is True
        assert w.stored("usr-1", conn.connection_id).state is NEEDS
        assert "needs_reconnect" in caplog.text

    async def test_a_gone_credential_still_flags_the_connection(self):
        w = World()
        conn = await w.connect()
        await w.store.repo.delete("usr-1", conn.credential_id)
        assert await w.service.flag_reconnect(conn.handle) is True
        assert w.stored("usr-1", conn.connection_id).state is NEEDS


class TestDisconnect:
    async def test_a_disconnect_of_the_primary_hands_it_to_the_oldest_other_active(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        third = await w.connect(subject="subject-c", identity="microsoft_onedrive:drive-c")
        await w.service.flag_reconnect(second.handle)
        assert await w.service.disconnect("usr-1", first.connection_id) is True
        stored = w.stored("usr-1", first.connection_id)
        assert stored.state is GONE and stored.disconnected_at is not None
        assert w.primary_id() == third.connection_id

    async def test_a_disconnect_of_the_last_drive_clears_the_primary(self):
        w = World()
        conn = await w.connect()
        assert await w.service.disconnect("usr-1", conn.connection_id) is True
        assert w.primary_id() is None
        assert await w.service.disconnect("usr-1", conn.connection_id) is False

    async def test_a_disconnect_of_another_drive_keeps_the_primary(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        assert await w.service.disconnect("usr-1", second.connection_id) is True
        assert w.primary_id() == first.connection_id

    async def test_an_absent_connection_is_no_transition(self):
        w = World()
        await w.connect()
        assert await w.service.disconnect("usr-1", "conn-x") is False
        assert await w.service.disconnect("usr-2", "conn-1") is False
        assert w.stored("usr-1", "conn-1").state is ACTIVE


class TestReconnectNeeded:
    async def test_flagged_and_credential_less_active_connections_are_owed_the_notice(self):
        w = World()
        healthy = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        flagged = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        orphan = await w.connect(subject="subject-c", identity="microsoft_onedrive:drive-c")
        gone = await w.connect(subject="subject-d", identity="microsoft_onedrive:drive-d")
        await w.service.flag_reconnect(flagged.handle)
        await w.store.repo.delete("usr-1", orphan.credential_id)
        await w.service.disconnect("usr-1", gone.connection_id)
        await w.store.repo.delete("usr-1", gone.credential_id)
        owed = await w.service.reconnect_needed("usr-1")
        assert [c.connection_id for c in owed] == [flagged.connection_id, orphan.connection_id]
        assert healthy.connection_id not in [c.connection_id for c in owed]

    async def test_nothing_is_owed_without_connections(self):
        assert await World().service.reconnect_needed("usr-1") == []

    async def test_the_existence_check_never_decrypts(self):
        w = World()
        await w.connect()

        async def no_decrypt(ciphertext, *, aad):
            raise RuntimeError("decrypt called")
        w.store.cipher.decrypt = no_decrypt
        assert await w.service.reconnect_needed("usr-1") == []

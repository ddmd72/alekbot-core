"""DriveConnectionService and the mount tree (MULTI_DRIVE_RFC §11.1, §11.8; slice 2a core): `activate` stores the first
`name_history` entry at the creation instant and names a second connection `Drive 2`; `mounts()` holds the live
connections only, marks the primary and carries held former names; `connection_of_tag` finds a connection in any
state; `UserDriveService.mounts` delegates. Over the in-memory connection repository; assertions on what is stored and
what readers return. Technical ids only; names from the invented universe."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
KIND = "fake_a"


def credential(subject: str, credential_id: str) -> OAuthCredential:
    return OAuthCredential(credential_id=credential_id, user_id="usr-1", account_id="acc-1", kind=KIND,
                           integration=None, account=ProviderAccount(realm="fake", subject=subject, label=""),
                           access_token="at", refresh_token="rt", token_expiry=T0, scopes=[])


class World:
    def __init__(self, now=None) -> None:
        self.repo = FakeDriveConnectionRepository()
        self.ticks = count()
        self.now = now
        ids = (f"conn-{n}" for n in count(1))
        tags = iter(["a1", "b7", "c3"])
        self.service = DriveConnectionService(
            self.repo, None,   # type: ignore[arg-type]   # the credential port is read only by flag_reconnect
            clock=self.clock, new_id=lambda: next(ids), new_tag=lambda: next(tags))

    def clock(self) -> datetime:
        return self.now if self.now is not None else T0 + timedelta(minutes=next(self.ticks))


class TestActivateRecordsTheName:
    async def test_the_first_connection_is_drive_with_one_history_entry_at_its_creation(self):
        w = World()
        conn = await w.service.activate(credential("subject-a", "cred-1"), "fake_a:drive-a")
        stored = w.repo.connections[("usr-1", conn.connection_id)]
        assert stored.mount_name == "Drive"
        assert stored.name_history == (MountNameChange("Drive", stored.created_at),)

    async def test_a_second_account_is_drive_2_with_its_own_entry(self):
        w = World()
        await w.service.activate(credential("subject-a", "cred-1"), "fake_a:drive-a")
        second = await w.service.activate(credential("subject-b", "cred-2"), "fake_a:drive-b")
        stored = w.repo.connections[("usr-1", second.connection_id)]
        assert stored.mount_name == "Drive 2"
        assert stored.name_history == (MountNameChange("Drive 2", stored.created_at),)

    async def test_a_reconnect_adds_no_entry(self):
        w = World()
        first = await w.service.activate(credential("subject-a", "cred-1"), "fake_a:drive-a")
        again = await w.service.activate(credential("subject-a", "cred-1"), "fake_a:drive-a")
        assert again.connection_id == first.connection_id
        assert w.repo.connections[("usr-1", first.connection_id)].name_history == first.name_history

    async def test_a_held_former_name_is_not_given_to_a_new_connection(self):
        w = World(now=T0 + timedelta(days=3))
        await seed(w.repo, "usr-1", "conn-9", tag="d4", created_at=T0)
        held = replace(w.repo.connections[("usr-1", "conn-9")], mount_name="Acme",
                       name_history=(MountNameChange("Drive", T0), MountNameChange("Acme", T0 + timedelta(days=1))))
        w.repo.connections[("usr-1", "conn-9")] = held
        conn = await w.service.activate(credential("subject-b", "cred-2"), "fake_a:drive-b")
        assert conn.mount_name == "Drive 2"


class TestMounts:
    async def test_live_connections_only_with_the_primary_marked(self):
        repo = FakeDriveConnectionRepository()
        await seed(repo, "usr-1", "conn-1", tag="a1", created_at=T0)
        await seed(repo, "usr-1", "conn-2", tag="b7", primary=False, created_at=T0 + timedelta(minutes=1),
                   state=ConnectionState.NEEDS_RECONNECT)
        await seed(repo, "usr-1", "conn-3", tag="c3", primary=False, created_at=T0 + timedelta(minutes=2),
                   state=ConnectionState.DISCONNECTED)
        repo.connections[("usr-1", "conn-2")] = replace(repo.connections[("usr-1", "conn-2")], mount_name="Acme")
        service = DriveConnectionService(repo, None, clock=lambda: T0)   # type: ignore[arg-type]
        table = await service.mounts("usr-1")
        assert [(m.name, m.connection_id, m.primary) for m in table.mounts] == [
            ("Drive", "conn-1", True), ("Acme", "conn-2", False)]
        assert table.primary().connection_id == "conn-1"

    async def test_former_names_are_carried_while_held(self):
        repo = FakeDriveConnectionRepository()
        await seed(repo, "usr-1", "conn-1", tag="a1", created_at=T0)
        await seed(repo, "usr-1", "conn-2", tag="b7", primary=False, created_at=T0 + timedelta(minutes=1))
        repo.connections[("usr-1", "conn-2")] = replace(
            repo.connections[("usr-1", "conn-2")], mount_name="Acme Hamburg",
            name_history=(MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T0 + timedelta(days=1))))
        held = await DriveConnectionService(repo, None, clock=lambda: T0 + timedelta(days=30)).mounts("usr-1")  # type: ignore[arg-type]
        gone = await DriveConnectionService(repo, None, clock=lambda: T0 + timedelta(days=31)).mounts("usr-1")  # type: ignore[arg-type]
        assert held.by_connection("conn-2").former_names == ("Acme",)
        assert gone.by_connection("conn-2").former_names == ()

    async def test_a_user_without_connections_has_an_empty_table(self):
        table = await DriveConnectionService(FakeDriveConnectionRepository(), None).mounts("usr-1")  # type: ignore[arg-type]
        assert (table.mounts, table.primary()) == ((), None)

    async def test_the_user_drive_service_delegates(self):
        repo = FakeDriveConnectionRepository()
        await seed(repo, "usr-1", "conn-1", tag="a1", created_at=T0)
        connections = DriveConnectionService(repo, None, clock=lambda: T0)   # type: ignore[arg-type]
        drives = UserDriveService(drive=None, connections=connections)   # type: ignore[arg-type]
        assert await drives.mounts("usr-1") == await connections.mounts("usr-1")


class TestConnectionOfTag:
    async def test_finds_a_connection_in_any_state(self):
        repo = FakeDriveConnectionRepository()
        await seed(repo, "usr-1", "conn-1", tag="a1", created_at=T0)
        await seed(repo, "usr-1", "conn-2", tag="b7", primary=False, state=ConnectionState.DISCONNECTED,
                   created_at=T0 + timedelta(minutes=1))
        service = DriveConnectionService(repo, None)   # type: ignore[arg-type]
        assert (await service.connection_of_tag("usr-1", "b7")).connection_id == "conn-2"
        assert (await service.connection_of_tag("usr-1", "a1")).connection_id == "conn-1"

    async def test_none_for_a_tag_the_user_never_had_or_another_users(self):
        repo = FakeDriveConnectionRepository()
        await seed(repo, "usr-2", "conn-1", tag="a1", created_at=T0)
        service = DriveConnectionService(repo, None)   # type: ignore[arg-type]
        assert await service.connection_of_tag("usr-1", "a1") is None
        assert await service.connection_of_tag("usr-2", "c3") is None

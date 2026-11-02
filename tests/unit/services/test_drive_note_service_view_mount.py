"""`DriveNoteService.get` prints the mount like the receipts do (MULTI_DRIVE_RFC §11.1; slice 2a E-11): the view's path is
`<mount>/<path>`; the stored snapshot stays connection-relative."""
from dataclasses import replace
from datetime import datetime, timezone

from tests.unit.services.test_drive_note_service_mounts import REL, _stored, world  # noqa: F401  (fixture)
from src.domain.drive_connection_state import ConnectionState

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


class TestGetPrintsTheMount:
    async def test_get_prints_the_mount_and_the_snapshot_stays_relative(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        note = await _stored(world)
        for ref in (world.ref("a1"), note.ref):
            view = await world.service.get("usr-1", ref)
            assert view.path == f"Drive/{REL}"
        assert (await _stored(world)).snapshot.path == REL

    async def test_a_disconnected_connection_prints_its_last_name(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("b2"), "the plan")
        await world.repo.update("usr-1", "conn-2", mount_name="Acme")
        stored = world.repo.connections[("usr-1", "conn-2")]
        world.repo.connections[("usr-1", "conn-2")] = replace(stored, state=ConnectionState.DISCONNECTED,
                                                              disconnected_at=T1)
        view = await world.service.get("usr-1", world.ref("b2"))
        assert view.path == f"Acme/{REL}"

    async def test_an_unknown_tag_keeps_the_relative_path(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        note = await _stored(world)
        world.repo.connections.pop(("usr-1", "conn-1"))
        view = await world.service.get("usr-1", note.ref)
        assert view.path == REL

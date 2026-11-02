"""A no-op write through a note ref (no live file) still prints the mount of the note's connection."""
from tests.unit.services.test_drive_note_service_mounts import REL, _stored, world  # noqa: F401


async def test_a_noop_set_through_a_note_ref_prints_the_mount(world):  # noqa: F811
    await world.service.set("usr-1", "acc-1", world.ref("b2"), "the plan")
    note = await _stored(world)
    again = await world.service.set("usr-1", "acc-1", f"drive_note:{note.note_id}", "the plan")
    assert again.noop and again.path == f"Drive 2/{REL}"

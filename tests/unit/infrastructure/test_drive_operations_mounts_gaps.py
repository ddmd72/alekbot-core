"""Slice 2a: a rename-only move (no destination) and a failed mount read in the preflight. Technical ids and the
invented cast only."""
from tests.unit.infrastructure.test_drive_operations_mounts import CTX, PLAN, world  # noqa: F401


async def test_a_rename_only_move_keeps_the_folder_and_needs_no_destination(world):  # noqa: F811
    out = await world.ops.move(CTX, "usr-1", f"drive:a1:{world.plan}", new_name="plan2.docx", wait_s=5)
    node = world.drive.tree(world.one.handle).nodes[world.plan]
    assert out.success and node.name == "plan2.docx" and node.parent_id == world.rd
    assert PLAN not in {n.name for n in world.drive.tree(world.one.handle).nodes.values()}


async def test_a_failed_mount_read_leaves_the_preflight_without_mount_lines(world):  # noqa: F811
    async def broken(user_id):
        raise RuntimeError("down")
    world.service.mounts = broken
    pre = await world.ops.preflight("usr-1")
    assert pre.mounts == ()

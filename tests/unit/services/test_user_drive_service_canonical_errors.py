"""UserDriveService refusals that name a path print it canonical (`<mount>/<path>`), read lazily on the raise path only
(MULTI_DRIVE_RFC §11.1, slice 2a E-6); a success reads no connection."""
import pytest

from src.domain.user_drive import DrivePathError
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter


class CountingRepo(FakeDriveConnectionRepository):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    async def get(self, user_id, connection_id):
        self.gets += 1
        return await super().get(user_id, connection_id)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = CountingRepo()
    w.drive = FakeDriveAdapter("fake_a")
    w.conn = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.h = w.conn.handle
    w.service = UserDriveService(w.drive, connection_service(w.repo))
    w.rd = w.drive.plant(w.h, "root", "R&D", folder=True)
    w.txt = w.drive.plant(w.h, w.rd, "notes.txt", b"n")
    w.docx = w.drive.plant(w.h, w.rd, "Tom's plan.docx", b"plan")
    w.bin = w.drive.plant(w.h, w.rd, "raw.txt", b"\xff\xfe")
    w.gets_before = 0
    return w


async def _refusal(coro) -> str:
    with pytest.raises(DrivePathError) as caught:
        await coro
    return str(caught.value)


async def test_a_missing_folder(world):
    assert await _refusal(world.service.list_folder(world.h, "R&D/Missing")) == "No folder 'Drive/R&D/Missing' on the drive"


async def test_a_name_taken_by_a_file(world):
    text = await _refusal(world.service.ensure_folder(world.h, "R&D/notes.txt/Sub"))
    assert text == "'Drive/R&D/notes.txt' already exists as a file, so it cannot be used as a folder"


async def test_a_file_where_a_folder_is_needed(world):
    assert await _refusal(world.service.list_folder_by_id(world.h, world.txt)) == \
        "'Drive/R&D/notes.txt' is a file, not a folder; open it instead."


async def test_append_refusals(world):
    assert await _refusal(world.service.append_text(world.h, world.docx, "x")) == \
        "'Drive/R&D/Tom's plan.docx' is not a text file; text can only be appended to text files"
    assert await _refusal(world.service.append_text(world.h, world.bin, "x")) == "'Drive/R&D/raw.txt' is not UTF-8 text"


async def test_replace_of_a_folder(world):
    assert await _refusal(world.service.replace_with(world.h, world.rd, b"x", "text/plain")) == \
        "'Drive/R&D' is a folder; only a file's content can be replaced"


async def test_a_folder_moved_into_itself(world):
    assert await _refusal(world.service.move(world.h, world.rd, "R&D/Sub", None)) == \
        "'Drive/R&D' cannot be moved into itself"


async def test_a_success_reads_no_connection(world):
    reads = world.repo.gets
    await world.service.list_folder(world.h, "R&D")
    await world.service.append_text(world.h, world.txt, "more")
    await world.service.move(world.h, world.txt, "R&D/Sub", None)
    assert world.repo.gets == reads


async def test_a_lookup_that_fails_leaves_the_relative_path(world):
    async def broken(user_id, connection_id):
        raise RuntimeError("down")
    world.repo.get = broken
    assert await _refusal(world.service.list_folder(world.h, "R&D/Missing")) == "No folder 'R&D/Missing' on the drive"

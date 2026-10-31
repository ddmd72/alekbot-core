"""`get_chunks_with_vectors` on the in-memory repository (USER_DRIVE_INDEXING_RFC §10.1 folder scope): the chunks of
one generation by index, each with its vector, missing indexes skipped."""
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveChunk
from tests.unit.services.drive_index_fakes import U_SCOPE


def chunk(item="a", gen=1, index=0, user="u"):
    return DriveChunk(user_id=user, connection_id=user, item_id=item, generation=gen, index=index, text=f"{item}/{gen}/{index}",
                      char_start=index * 10, char_end=index * 10 + 10, kind="pdf")


async def test_chunks_with_vectors_by_index():
    repo = InMemoryDriveIndexRepository()
    await repo.write_chunks([chunk(index=2), chunk(index=0), chunk(gen=2, index=1), chunk(user="v", index=1)],
                            [[0.2, 1.0], [0.0, 1.0], [9.0, 9.0], [7.0, 7.0]])
    rows = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [2, 1, 0, 2])
    assert [(c.index, v) for c, v in rows] == [(0, [0.0, 1.0]), (2, [0.2, 1.0])]


async def test_the_returned_vector_is_a_copy():
    repo = InMemoryDriveIndexRepository()
    await repo.write_chunks([chunk()], [[1.0, 0.0]])
    (row,) = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [0])
    row[1][0] = 5.0
    assert repo.chunks[("u", "a", 1, 0)][1] == [1.0, 0.0]

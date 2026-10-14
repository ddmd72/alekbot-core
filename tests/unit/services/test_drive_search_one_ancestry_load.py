"""The ancestors of every stream's candidates load in one pass (USER_DRIVE_INDEXING_RFC §10.1 step 4, review D minor).

Loading them per stream ran up to four sequential rounds of `get_records`, one per stream, each up to the chain
depth; the union of the four streams' records needs only one round per level."""
from .test_drive_search_content_streams import FAR, ContentSpy, by_id, index, query_vector, settle_metadata
from .test_drive_search_service import World


class RecordsSpy(ContentSpy):
    def __init__(self) -> None:
        super().__init__()
        self.record_reads = []

    async def get_records(self, user_id, item_ids):
        self.record_reads.append(tuple(item_ids))
        return await super().get_records(user_id, item_ids)


def world():
    w = World()
    spy = RecordsSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


async def test_the_four_streams_load_their_ancestors_together(monkeypatch):
    w = world()
    folders = [w.drive.add(f"F{n}", folder=True) for n in range(4)]
    by_card = w.drive.add("a.pdf", parent=folders[0])
    by_name = w.drive.add("lease.pdf", parent=folders[1])
    by_chunk = w.drive.add("b.pdf", parent=folders[2])
    by_term = w.drive.add("c.pdf", parent=folders[3])
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "lease 98765")
    for item in (by_name, by_chunk, by_term):   # out of the card stream: each stream holds a different file
        w.repo.vectors.pop(("u", item), None)
    w.repo.vectors[("u", by_card)] = list(q)
    await index(w, by_chunk, ["the lease terms"], [q])
    await index(w, by_term, ["reference 98765"], [FAR])
    w.repo.record_reads.clear()
    result = await w.search("lease 98765")
    assert {by_card, by_name, by_chunk, by_term} <= set(by_id(result))
    ancestor_reads = [ids for ids in w.repo.record_reads if set(ids) & set(folders)]
    assert len(ancestor_reads) == 1 and set(ancestor_reads[0]) == set(folders)

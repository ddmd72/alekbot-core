"""Passages across connections (multi-drive 2b P-C): one read per searched drive, merged by best chunk distance,
identical content once, the ledger's keys read per connection tag."""
from dataclasses import replace
from typing import List

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_search import STATUS_DRIVE_REFUSED, STATUS_OK

from .drive_two_drives_harness import ACME_CONN, TwoDrives

QUESTION = "how does the review process work"
SAME = ContentIdentity("fakehash", "hsame", 40)


def toward(d: float) -> List[float]:
    return [1.0, d, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class Passages(TwoDrives):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.emb.pinned[QUESTION] = toward(0.0)

    def doc(self, name: str, texts, distances, *, parent: str = "root") -> str:
        item = self.file(name, parent=parent, kind="docx", chunks=texts)
        for index, d in enumerate(distances):
            chunk, _ = self.repo.chunks[("u", item, 1, index)]
            self.repo.chunks[("u", item, 1, index)] = (chunk, toward(d))
        return item

    async def passages(self, *, drives: str = "", shown=frozenset(), **fields):
        return await self.service.excerpts("u", ExcerptRequest(question=QUESTION, **fields), drives=drives, shown=shown)


async def _world() -> Passages:
    w = await Passages().open()
    w.plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3])
    w.on_drive_only = w.doc("menu.docx", ["Lunch options near the office."], [5.0])
    w.acme_best = w.on_acme(w.doc("Acme review.docx", ["Acme signs off each review."], [0.1]))
    return w


async def test_empty_drives_reads_both_drives_best_distance_first_with_each_files_tag():
    w = await _world()
    result = await w.passages()
    assert result.status == STATUS_OK and result.not_searched_drives == ()
    assert [(f.ref_tag, f.path) for f in result.files][:2] == [("b2", "Acme/Acme review.docx"),
                                                             ("a1", "Drive/Tom's plan.docx")]
    assert result.files[0].ref == f"drive:b2:{w.acme_best}"


async def test_a_named_drive_is_the_only_one_read():
    w = await _world()
    result = await w.passages(drives="Drive")
    assert {f.ref_tag for f in result.files} == {"a1"} and result.ref_tag == "a1"
    assert result.not_searched_drives == ("Acme",)


async def test_identical_content_on_both_drives_is_one_file_with_the_other_path():
    w = await _world()
    w.set(w.plan, content_identity=SAME)
    twin = w.on_acme(w.doc("Tom's plan.docx", ["The review is held weekly."], [0.2]), identity=SAME)
    result = await w.passages()
    plans = [f for f in result.files if f.name == "Tom's plan.docx"]
    assert len(plans) == 1
    assert plans[0].ref_tag == "a1" and plans[0].also_at == ("Acme/Tom's plan.docx",) and twin


async def test_shown_keys_are_read_per_tag():
    w = await _world()
    first = await w.passages()
    acme_file = next(f for f in first.files if f.ref_tag == "b2")
    key = (acme_file.item_id, acme_file.generation, acme_file.chunks[0].index)
    again = await w.passages(shown=frozenset({("b2", key)}))
    assert acme_file.item_id not in {f.item_id for f in again.files if f.ref_tag == "b2"}
    other_tag = await w.passages(shown=frozenset({("a1", key)}))      # the same ids under the other tag hide nothing
    assert acme_file.item_id in {f.item_id for f in other_tag.files}


async def test_a_drive_that_cannot_be_read_is_named_while_the_other_answers():
    w = Passages()
    w.acme = replace(w.acme, indexing_enabled=False)
    await w.open()
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3])
    result = await w.passages()
    assert result.status == STATUS_OK and {f.ref_tag for f in result.files} == {"a1"}
    assert result.not_searched_drives == ("Acme",)


async def test_a_folder_on_another_drive_than_drives_is_refused_and_unknown_names_too():
    w = await _world()
    assert (await w.passages(drives="Drive", folder="Acme/R&D")).status == STATUS_DRIVE_REFUSED
    assert (await w.passages(drives="Acme Lviv")).status == STATUS_DRIVE_REFUSED


async def test_ids_exist_on_acme_only():
    w = await _world()
    assert ("u", w.acme_best) not in w.repo.records and (ACME_CONN, w.acme_best) in w.repo.records

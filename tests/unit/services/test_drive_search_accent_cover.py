"""A name typed with or without its accents ranks first: the unaccented spelling is part of the full-name cover
tie-break, not only of the name stream (USER_DRIVE_INDEXING_RFC §10.1 step 3a, §3.2)."""
import pytest

from .test_drive_search_service import World, names


def _cards_prefer_fillers(w, target_stem):
    """The query vector is close to every filler card and far from the target's, so the fillers lead the card
    stream and also sit in the name stream: only the cover tie-break can put the target first."""
    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if target_stem in text:
            return [0.0, 1.0] + [0.0] * 6
        return [1.0, 0.01] + [0.0] * 6
    w.emb.get_embedding = emb


@pytest.mark.parametrize("query,target,filler", [
    ("resume", "Résumé.pdf", "Resumen ventas {i}.txt"),
    ("résumé", "Resume.pdf", "Resumen ventas {i}.txt"),
    ("coeur", "Cœur.pdf", "Coeurdelion {i}.txt"),
    ("ano", "Año.pdf", "Anotaciones {i}.txt"),
])
async def test_the_target_is_covered_and_ranked_first(query, target, filler):
    w = World()
    _cards_prefer_fillers(w, target.rsplit(".", 1)[0])
    for i in range(12):
        w.drive.add(filler.format(i=i))
    w.drive.add(target)
    await w.settle()
    assert names(await w.search(query))[0] == target

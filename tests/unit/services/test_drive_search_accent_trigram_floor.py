"""An accented token must not reach the NAME_MIN_TRIGRAMS floor through three script forms of one shared trigram
(original, unaccented, Cyrillic transliteration of the unaccented form): only the transliteration of the token as
written carries trigrams (USER_DRIVE_INDEXING_RFC §10.1 step 3a)."""
from src.domain.drive_name_keys import name_keys, name_match, query_name_keys

from .test_drive_search_service import World, names


def _cards_prefer_fillers(w, target_word):
    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if target_word in text:
            return [0.0, 1.0] + [0.0] * 6
        return [1.0, 0.01] + [0.0] * 6
    w.emb.get_embedding = emb


def test_one_shared_accented_trigram_stays_below_the_floor():
    assert name_match(name_keys("Amédée 001.txt"), query_name_keys("Médic")) is None
    assert name_match(name_keys("Médico informe.docx"), query_name_keys("Médic")) is not None


async def test_the_target_is_not_pushed_out_by_160_accented_fillers():
    w = World(page_size=10_000)
    _cards_prefer_fillers(w, "Médico")
    for i in range(160):
        w.drive.add(f"Amédée {i:03d}.txt")
    w.drive.add("Médico informe.docx")
    await w.settle()
    assert "Médico informe.docx" in names(await w.search("Médic"))

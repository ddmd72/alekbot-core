"""Term selectivity past the first page (USER_DRIVE_INDEXING_RFC §24.6, arbiter R2 S-M1). The term page is ordered
by item_id: a few long files sorting first can fill it while the term sits in many more files past it. A full first
page from at most TERM_NONSELECTIVE_FILES files is therefore probed past its last file, one new file per probe, until
one file too many is seen or the term runs out; only then is it selective."""
from unittest.mock import MagicMock

from src.domain.drive_index_params import CONTENT_TERM_FETCH, FETCH_MAX, TERM_NONSELECTIVE_FILES
from src.services import drive_search_service as search_mod
from src.services.drive_search_service import _TermPages, _Stats

from .test_drive_search_content_streams import FAR, ContentSpy, by_id, index, settle_metadata
from .test_drive_search_service import World


class ProbeSpy(ContentSpy):
    def __init__(self) -> None:
        super().__init__()
        self.probes = []

    async def find_next_file_with_term(self, user_id, term, after_item_id, kind=None):
        self.probes.append((term, after_item_id, kind))
        return await super().find_next_file_with_term(user_id, term, after_item_id, kind)


def world():
    w = World()
    spy = ProbeSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


def _vectors_off(w):
    """Cards and chunk vectors in another space: only the name and exact-term streams run."""
    state = w.repo.states["u"]
    w.repo.states["u"] = type(state)(**{**state.__dict__, "applied_space": "another-space/8"})


async def test_a_year_filling_the_page_from_four_long_files_is_not_selective(monkeypatch):
    """The reviewer's case: "2024" in 30 chunks of each of 4 long reports that sort first, and once in 40 memos past
    them. The first page of 100 spans 4 files — by id order only. The probe finds the memos: "2024" is held by many
    files, takes no part, and is not re-read wider. The contract number alone selects."""
    w = world()
    heavy = [w.drive.add(f"a-heavy-{i}.pdf") for i in range(4)]
    light = [w.drive.add(f"m-light-{i:02d}.pdf") for i in range(40)]
    target = w.drive.add("z-contract.pdf")
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in heavy:
        await index(w, doc, [f"annual report 2024 part {p}" for p in range(30)], [FAR] * 30)
    for doc in light:
        await index(w, doc, ["memo 2024"], [FAR])
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search("2024 98765")
    assert [c.item_id for c in result.cards] == [target]
    assert "98765" in by_id(result)[target].window
    assert ("2024", FETCH_MAX, None) not in w.repo.term_calls          # a non-selective term is never read wider
    probes = [p for p in w.repo.probes if p[0] == "2024"]
    assert len(probes) == TERM_NONSELECTIVE_FILES + 1 - 4              # stops at one file too many
    assert probes[0][1] == heavy[-1]                                   # past the page's last file
    assert f"term_probes={len(w.repo.probes)}" in log.info.call_args[0][0]


async def test_a_full_page_from_few_files_with_few_more_past_it_still_selects(monkeypatch):
    """An IBAN in every chunk of two long contracts and in three more letters: 5 files hold it, at most 8 — it selects
    and, its page being full, is read wider."""
    w = world()
    contracts = [w.drive.add(f"a-contract-{i}.pdf") for i in range(2)]
    letters = [w.drive.add(f"b-letter-{i}.pdf") for i in range(3)]
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    groups = ("XX00", "1234", "5678", "9012")   # synthetic; built from groups, never one literal
    iban = " ".join(groups)
    for doc in contracts:
        await index(w, doc, [f"pay to {iban} clause {p}" for p in range(60)], [FAR] * 60)
    for doc in letters:
        await index(w, doc, [f"remit to {iban}"], [FAR])
    result = await w.search(iban)
    assert set(by_id(result)) == set(contracts) | set(letters)
    assert all(c.window for c in result.cards)
    assert [p[1] for p in w.repo.probes] == [contracts[-1], *letters]   # exhausted after the last letter
    assert ("".join(groups).casefold(), FETCH_MAX, None) in w.repo.term_calls


async def test_probes_are_not_repeated_on_the_widening():
    """The probe decides once, on the first read; the widening re-reads the page only."""
    w = world()
    spy = w.repo
    svc = w.search_svc
    pages = _TermPages()
    stats = _Stats()

    async def full_page(user_id, term, limit, kind=None):
        spy.term_calls.append((term, limit, kind))
        return [type("C", (), {"item_id": "a"})() for _ in range(limit)]   # one file fills any page

    async def none_past(user_id, term, after_item_id, kind=None):
        spy.probes.append((term, after_item_id, kind))
        return None

    spy.find_chunks_by_term = full_page
    spy.find_next_file_with_term = none_past
    await svc._term_hits("u", ["iban"], CONTENT_TERM_FETCH, None, pages, stats)
    await svc._term_hits("u", ["iban"], FETCH_MAX, None, pages, stats)
    assert spy.probes == [("iban", "a", None)]
    assert spy.term_calls == [("iban", CONTENT_TERM_FETCH, None), ("iban", FETCH_MAX, None)]
    assert "iban" not in pages.non_selective and stats.term_probes == 1

"""DriveSearchService: the name-key pool, filter-only honesty, folder lookup, the walk budget and widening
(USER_DRIVE_INDEXING_RFC §10.1, §10.2)."""

import pytest

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index_params import NAME_KEY_QUERIES
from src.domain.drive_keys import query_concepts
from src.domain.drive_tier_pool import concept_trigrams
from src.domain.drive_search import STATUS_FOLDER_NOT_FOUND, STATUS_OK, render_results
from src.services import drive_search_service as search_mod

from .test_drive_search_service import World, names, paths, taken


@pytest.fixture
def w():
    return World()


def _orthogonal_embeddings(w, target_word):
    """The query vector is far from the target's card and close to every filler card, so the card kNN
    (CARD_FETCH = 50) never rescues the target: only the name stream can find it."""
    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if target_word in text:
            return [0.0, 1.0] + [0.0] * 6
        return [1.0, 0.01] + [0.0] * 6
    w.emb.get_embedding = emb


def _limit_calls(w, method, most):
    """Fail instead of looping: a stream asked more than `most` times means widening did not stop."""
    original = getattr(w.repo, method)
    calls = []

    async def guarded(*args, **kwargs):
        calls.append(args)
        if len(calls) > most:
            raise AssertionError(f"{method} called more than {most} times: widening is not bounded")
        return await original(*args, **kwargs)
    setattr(w.repo, method, guarded)


# --- Every trigram key gets its own capped call (RFC Δ17) -----------------------------------------

class TestTrigramPool:
    @pytest.mark.slow
    @pytest.mark.parametrize("query,target,filler,word", [
        ("leese agrement", "Lease agreement.pdf", "parent_{i:03d}.txt", "Lease"),   # every token misspelled
        ("Догов", "Договор аренды.docx", "Подогрев {i:03d}.txt", "Договор"),        # a prefix
        ("contrsto", "contrato alquiler.pdf", "conejo {i:03d}.txt", "contrato"),    # one misspelled word
    ])
    async def test_trigram_only_target_among_150_trigram_sharing_files(self, query, target, filler, word):
        w = World(page_size=10_000)
        _orthogonal_embeddings(w, word)
        for i in range(160):   # each shares 1-2 common trigrams with the query (below the floor), lower ids
            w.drive.add(filler.format(i=i))
        w.drive.add(target)    # the last item id: past the first 100 matches of any shared common trigram
        await w.settle()
        assert target in names(await w.search(query))

    async def test_long_query_singles_at_most_name_key_queries_trigrams_in_query_order_plus_one_rest(self, w):
        # §25.5.2 (spec change): trigrams find similar names and never cover, so there is no remainder batch; the
        # NAME_KEY_QUERIES singles go round-robin over the words, so a long query still probes every word.
        w.drive.add("a.pdf")
        await w.settle()
        query = "quarterly planning review document"
        before = len(w.repo.name_keys)
        await w.search(query)
        batches = w.repo.name_keys[before:]
        lists = [list(concept_trigrams(c)) for c in query_concepts(query).concepts if not c.is_number]
        assert len(lists) >= 3 and sum(map(len, lists)) > NAME_KEY_QUERIES
        expected = []
        for i in range(max(map(len, lists))):
            expected += [g[i] for g in lists if i < len(g) and g[i] not in expected]
        singles = [b[0] for b in batches if len(b) == 1 and b[0].startswith("t:")]
        assert singles == expected[:NAME_KEY_QUERIES]
        assert {g[0] for g in lists} <= set(singles)   # every word probed
        assert not [b for b in batches if len(b) > 1 and any(k.startswith("t:") for k in b)]


# --- A capped filter-only page with no survivors is not "no matches" ----------------------------

class TestFilterOnlyHonesty:
    async def test_filter_only_in_a_large_folder_does_not_claim_no_matches(self, w, monkeypatch):
        archive = w.drive.add("Archive", folder=True)
        for i in range(3):
            w.drive.add(f"scan{i}.pdf", parent=archive, media=taken(f"2019-01-0{i + 1}"))
        for i in range(5):
            w.drive.add(f"new{i}.pdf", media=taken(f"2026-01-0{i + 1}"))
        await w.settle()
        monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 2)   # the subtree walk is incomplete
        monkeypatch.setattr(search_mod, "CARD_FETCH", 2)
        monkeypatch.setattr(search_mod, "FETCH_MAX", 4)
        result = await w.search("", folder="Archive")
        assert result.cards == () and result.matched_at_least
        text = render_results(result)
        assert "No matching files or folders" not in text
        assert "does not mean there are none" in text and "Browse the folder, or narrow the dates." in text


# --- One folder from a full name page is a guess (§10.1 "instead of guessing") ------------------

class TestFolderLookupNeverGuesses:
    async def test_single_suffix_match_from_a_full_name_page_could_not_resolve(self, w, monkeypatch):
        fin = w.drive.add("Finance", folder=True)
        t1 = w.drive.add("2025", parent=w.drive.add("Taxes", parent=fin, folder=True), folder=True)
        w.drive.add("march.pdf", parent=t1)
        for i in range(3):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        old = w.drive.add("Old", folder=True)
        t2 = w.drive.add("2025", parent=w.drive.add("Taxes", parent=old, folder=True), folder=True)
        w.drive.add("march.pdf", parent=t2)
        await w.settle()
        monkeypatch.setattr(search_mod, "NAME_FETCH", 3)   # the page holds Finance/Taxes/2025 but not Old/...
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_FOLDER_NOT_FOUND and result.folder_lookup_truncated
        assert result.cards == ()

    async def test_single_suffix_match_from_a_page_that_was_not_full_is_used(self, w):
        fin = w.drive.add("Finance", folder=True)
        t1 = w.drive.add("2025", parent=w.drive.add("Taxes", parent=fin, folder=True), folder=True)
        w.drive.add("march.pdf", parent=t1)
        await w.settle()
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_OK and paths(result) == ["Finance/Taxes/2025/march.pdf"]

    async def test_single_exact_path_match_from_a_full_name_page_is_used(self, monkeypatch):
        """An exact full path is unique in a drive: one exact match is not a guess even when the name page
        was full. The root descent is cut by its budget, so only the name lookup can find the folder."""
        w = World(page_size=10_000)
        taxes = w.drive.add("Taxes", folder=True)
        t = w.drive.add("2025", parent=taxes, folder=True)          # lowest '2025' id: inside the capped page
        w.drive.add("march.pdf", parent=t)
        for i in range(4):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        w.drive.add("march.pdf")
        await w.settle()
        monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 3)   # the root has more children: descent cut
        monkeypatch.setattr(search_mod, "NAME_FETCH", 2)           # the name page for 'w:2025' is full
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_OK and paths(result) == ["Taxes/2025/march.pdf"]

    async def test_single_suffix_match_with_a_cut_descent_and_a_full_name_page_is_refused(self, monkeypatch):
        w = World(page_size=10_000)
        fin = w.drive.add("Finance", folder=True)
        t = w.drive.add("2025", parent=w.drive.add("Taxes", parent=fin, folder=True), folder=True)
        w.drive.add("march.pdf", parent=t)
        for i in range(4):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        await w.settle()
        monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 3)
        monkeypatch.setattr(search_mod, "NAME_FETCH", 2)
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_FOLDER_NOT_FOUND and result.folder_lookup_truncated


# --- The subtree walk stops after FOLDER_INMEM_CARDS records (§10.1 Folder) ---------------------

class TestWalkBudget:
    @pytest.mark.slow
    async def test_walk_reads_at_most_the_budget_plus_one_page_per_parallel_listing(self, monkeypatch):
        w = World(page_size=10_000)
        photos = w.drive.add("Photos", folder=True)
        for year in range(20):
            folder = w.drive.add(f"{2000 + year}", parent=photos, folder=True)
            for i in range(10):
                w.drive.add(f"p{year}_{i}.jpg", parent=folder, mime_type="image/jpeg")
        await w.settle()
        monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 30)
        monkeypatch.setattr(search_mod, "_CHILD_PAGE", 2)
        original = w.repo.children
        read = {}

        async def children(user_id, parent_id, after, limit):
            page = await original(user_id, parent_id, after, limit)
            read[parent_id] = read.get(parent_id, 0) + len(page)
            return page
        w.repo.children = children
        result = await w.search("p3", folder=f"drive:a1:{photos}")
        assert result.status == STATUS_OK and w.repo.card_limits   # incomplete walk: the user-wide streams ran
        walked = sum(read.values())
        assert walked <= 30 + 1 + search_mod._WALK_PARALLEL * 2, walked


# --- Search decisions, each pinned by its own test ------------------------------------------------

class TestSearchDecisions:

    async def test_filter_only_widens_at_most_once_even_when_the_widened_page_is_full(self, w, monkeypatch):
        monkeypatch.setattr(search_mod, "CARD_FETCH", 2)
        monkeypatch.setattr(search_mod, "FETCH_MAX", 3)
        for i in range(10):
            w.drive.add(f"scan {i:02d}.pdf", media=taken(f"2024-01-{i + 1:02d}"))
        await w.settle()
        _limit_calls(w, "filter_records", 4)
        await w.search("", kind="pdf")
        assert w.repo.filter_limits == [2, 3]

    async def test_a_duplicate_below_the_k_cut_still_adds_its_path(self, w):
        """The identical copy is the 10th newest; the shown card still lists where it is."""
        same = ContentIdentity("quickxor", "QX1", 10)
        old = w.drive.add("Old", folder=True)
        w.drive.add("copy.pdf", parent=old, size_bytes=10, content_identity=same, media=taken("2026-01-01"))
        for day in range(2, 10):
            w.drive.add(f"scan{day}.pdf", media=taken(f"2026-01-{day:02d}"))
        w.drive.add("original.pdf", size_bytes=10, content_identity=same, media=taken("2026-01-10"))
        await w.settle()
        result = await w.search("", kind="pdf")
        assert len(result.cards) == 8 and result.cards[0].name == "original.pdf"
        assert result.cards[0].also_at == ("Old/copy.pdf",)

    async def test_matched_at_least_when_the_widened_fetch_was_full(self, w, monkeypatch):
        """The count is a lower bound once the fetch hit its cap."""
        monkeypatch.setattr(search_mod, "CARD_FETCH", 2)
        monkeypatch.setattr(search_mod, "FETCH_MAX", 3)
        for i in range(5):
            w.drive.add(f"scan{i}.pdf", media=taken(f"2026-01-0{i + 1}"))
        await w.settle()
        result = await w.search("", kind="pdf")
        assert result.matched_total == 3 and result.matched_at_least
        assert "at least 3 files and folders match the filters" in render_results(result)

    async def test_a_scope_folder_needs_no_card_of_its_own(self, w):
        """A folder without a card (index full, capped) still scopes a search, by ref and by path."""
        folder = w.drive.add("Contracts", folder=True)
        w.drive.add("lease.pdf", parent=folder)
        w.drive.add("lease.pdf")
        await w.settle()
        w.set_state(folder, has_card=False)
        by_ref = await w.search("lease", folder=f"drive:a1:{folder}")
        assert by_ref.status == STATUS_OK and paths(by_ref) == ["Contracts/lease.pdf"]
        by_path = await w.search("lease", folder="Contracts")
        assert by_path.status == STATUS_OK and paths(by_path) == ["Contracts/lease.pdf"]

    async def test_could_not_resolve_from_a_full_name_page_alone(self, w, monkeypatch):
        """The descent read everything and found nothing; only the name page was full."""
        for i in range(6):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        await w.settle()
        monkeypatch.setattr(search_mod, "NAME_FETCH", 2)
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_FOLDER_NOT_FOUND and result.folder_lookup_truncated

    async def test_family_filter_only_widens_when_one_member_page_is_full(self, w, monkeypatch):
        """Any full member page makes the count a lower bound and widens the fetch."""
        monkeypatch.setattr(search_mod, "CARD_FETCH", 2)
        monkeypatch.setattr(search_mod, "FETCH_MAX", 4)
        for i in range(5):
            w.drive.add(f"budget{i}.xlsx", media=taken(f"2026-02-0{i + 1}"))
        w.drive.add("data.csv", media=taken("2026-01-01"))
        await w.settle()
        result = await w.search("", kind="spreadsheet")
        assert len(result.cards) == 5 and result.matched_total == 5 and result.matched_at_least
        assert 4 in w.repo.filter_limits

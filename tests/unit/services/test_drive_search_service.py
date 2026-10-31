"""DriveSearchService over an index built by the real tick (USER_DRIVE_INDEXING_RFC §10.1, §10.2, §17.6)."""
import itertools
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index import EpochPhase, RecordState
from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_search import (
    STATUS_AMBIGUOUS_FOLDER, STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_FOLDER_NOT_FOUND, STATUS_OK,
    DriveSearchRequest, render_results,
)
from src.domain.user_drive import DriveConnection
from src.services import drive_search_service as search_mod
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed

from .drive_harness_connections import harness_connections
from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE


class SpyRepo(InMemoryDriveIndexRepository):
    """Records the fetch limits each stream was asked for (widening, §10.1 step 5)."""

    def __init__(self) -> None:
        super().__init__()
        self.card_limits = []
        self.name_limits = []
        self.name_keys = []
        self.filter_limits = []
        self.filter_kinds = []

    async def find_nearest_cards(self, user_id, vector, limit, kind=None):
        self.card_limits.append(limit)
        return await super().find_nearest_cards(user_id, vector, limit, kind)

    async def find_by_name_keys(self, user_id, keys, limit, kind=None):
        self.name_limits.append((limit, kind))
        self.name_keys.append(tuple(keys))
        return await super().find_by_name_keys(user_id, keys, limit, kind)

    async def filter_records(self, user_id, kind, date_from, date_to, limit):
        self.filter_limits.append(limit)
        self.filter_kinds.append(kind)
        return await super().filter_records(user_id, kind, date_from, date_to, limit)


class World:
    def __init__(self, page_size=50, connections=None):
        self.drive = FakeContentDrive(page_size=page_size)   # wired as in production; see test_drive_indexing_service
        self.repo = SpyRepo()
        self.emb = FakeEmbeddings()
        self.users = FakeUsers()
        self.clock = Clock()
        self.indexer = DriveIndexingService(self.drive, self.repo, self.emb, FakeQueue(), self.users,
                                            lambda lat, lon: "Rome (Roma), Italy", "test-space/8",
                                            extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                            triage=FakeTriage(), connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)
        counter = itertools.count(1)
        self.search_svc = DriveSearchService(self.repo, self.emb, self.drive,
                                             connections or harness_connections(self.users, self.drive),
                                             clock=self.clock,
                                             nonce=lambda: f"nonce{next(counter):04d}",
                                             embedding_space="test-space/8")

    async def settle(self, max_ticks=50):
        for _ in range(max_ticks):
            await self.indexer.tick(U_SCOPE)
            state = await self.repo.get_sync_state(U_SCOPE)
            if state is None or state.continuation_enqueued_at is None:
                return
            self.clock.advance(1)
        raise AssertionError("did not settle")

    async def search(self, query="", **kw):
        return await self.search_svc.search("u", DriveSearchRequest(query=query, **kw))

    def set_state(self, item_id, **kw):
        key = ("u", item_id)
        self.repo.records[key] = replace(self.repo.records[key], **kw)


def taken(day):
    return DriveMediaFacts(taken_at=datetime.fromisoformat(f"{day}T10:00:00+00:00"))


def paths(result):
    return [c.path for c in result.cards]


@pytest.fixture
def w():
    return World()


# --- gate (§10.1 step 0) ------------------------------------------------------------------------

class TestGate:
    async def test_disabled_is_read_uncached_on_every_search(self, w):
        w.drive.add("lease.pdf")
        await w.settle()
        assert (await w.search("lease")).status == STATUS_OK
        reads = w.users.reads
        w.users.enabled["u"] = False   # opt-out with a warm agent cache: the next search sees it
        result = await w.search("lease")
        # the switch is the connection's (MULTI_DRIVE_RFC §3): a search reads the primary pointer and the connection
        # fresh (§4.4, E2), and the harness derives each read from the profile
        assert result.status == STATUS_DISABLED and result.cards == () and w.users.reads == reads + 2
        assert "turned on" in render_results(result)

    async def test_disconnect_hides_the_index(self):
        # a disconnect is the connection's state now (MULTI_DRIVE_RFC §4.4), not the drive port's
        conns = FakeDriveConnectionRepository()
        w = World(connections=connection_service(conns))
        w.drive.add("lease.pdf")
        await w.settle()
        await seed(conns, "u", "u", tag="a1", drive_identity="fake:d1", state=ConnectionState.DISCONNECTED)
        assert (await w.search("lease")).status == STATUS_DISCONNECTED

    async def test_expired_grant_answers_with_reconnect_needed_until_the_ttl(self, w):
        w.drive.add("lease.pdf")
        await w.settle()
        w.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
        await w.indexer.tick(U_SCOPE)   # the gate stamps access_lost_at
        result = await w.search("lease")
        assert result.status == STATUS_OK and result.envelope.reconnect_needed and paths(result) == ["lease.pdf"]
        assert "reconnect the drive" in render_results(result)
        w.clock.advance(ACCESS_LOSS_TTL.total_seconds())
        assert (await w.search("lease")).status == STATUS_DISCONNECTED

    async def test_other_drive_is_disconnected_until_the_purge(self, w):
        w.drive.add("lease.pdf")
        await w.settle()
        w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
        assert (await w.search("lease")).status == STATUS_DISCONNECTED

    async def test_enabled_before_the_first_tick_is_building(self, w):
        result = await w.search("lease")
        assert result.status == STATUS_OK and result.cards == () and result.envelope.building
        text = render_results(result)
        # The building fact stays; the standing guidance moved to Smart's protocol token (§10.4, §24.10)
        assert "The index is still being built: some files and folders may not be in it yet." in text
        assert "do not say a file does not exist" not in text


# --- envelope (§10.2) ---------------------------------------------------------------------------

class TestEnvelope:
    async def test_settled_index_is_not_building_and_shows_index_as_of(self, w):
        w.drive.add("doc.pdf")
        await w.settle()
        result = await w.search("doc")
        state = await w.repo.get_sync_state(U_SCOPE)
        assert not result.envelope.building and result.envelope.index_as_of == state.observed_as_of
        assert result.envelope.content_search is True and result.envelope.enabled   # the flip (§24.1, §10.2)

    @pytest.mark.parametrize("change", [
        dict(epoch_phase=EpochPhase.ENUMERATING), dict(epoch_phase=EpochPhase.SWEEPING),
    ])
    async def test_an_epoch_not_idle_is_building(self, w, change):
        w.drive.add("doc.pdf")
        await w.settle()
        w.repo.states["u"] = replace(await w.repo.get_sync_state(U_SCOPE), **change)
        assert (await w.search("doc")).envelope.building

    async def test_edits_and_cascades_do_not_set_building(self, w):
        item = w.drive.add("doc.pdf")
        await w.settle()
        w.set_state(item, priority=0, due_at=w.clock.now)
        assert not (await w.search("doc")).envelope.building
        w.set_state(item, priority=1, due_at=w.clock.now)
        assert not (await w.search("doc")).envelope.building

    async def test_a_first_seen_or_relisting_record_due_sets_building(self, w):
        """Class 2 holds first-seen records and re-listings (RFC Δ18): one limit-1 query decides."""
        folder = w.drive.add("Old", folder=True)
        await w.settle()
        assert not (await w.search("old")).envelope.building
        w.set_state(folder, reobserve_pending=True, priority=2, due_at=w.clock.now)
        assert (await w.search("old")).envelope.building

    async def test_a_purge_in_progress_answers_building_with_no_results(self, w):
        w.drive.add("lease.pdf")
        await w.settle()
        w.repo.states["u"] = replace(await w.repo.get_sync_state(U_SCOPE), purging=True)
        result = await w.search("lease")
        assert result.status == STATUS_OK and result.cards == () and result.envelope.building

    async def test_reconnected_after_the_ttl_answers_building_not_the_half_purged_index(self, w):
        w.drive.add("lease.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, access_lost_at=w.clock.now - ACCESS_LOSS_TTL)
        result = await w.search("lease")
        assert result.status == STATUS_OK and result.cards == () and result.envelope.building
        assert result.envelope.index_as_of is None

    async def test_unread_comes_from_the_counters(self, w):
        w.drive.add("doc.pdf")
        await w.settle()
        state = await w.repo.get_sync_state(U_SCOPE)
        w.repo.states["u"] = replace(state, counters={**state.counters, "unread": 3})
        assert (await w.search("doc")).envelope.unread == 3

    async def test_index_full_from_the_carded_counter(self, w, monkeypatch):
        w.drive.add("a.pdf")
        w.drive.add("b.pdf")
        await w.settle()
        assert not (await w.search("a")).envelope.index_full
        monkeypatch.setattr(search_mod, "MAX_CARD_RECORDS_PER_USER", 2)   # counters["carded"] == 2 after settle
        assert (await w.search("a")).envelope.index_full


# --- streams and fusion (§10.1 steps 1-7) --------------------------------------------------------

class TestRanking:
    @pytest.mark.parametrize("query,expected", [
        ("IMG_4912", "Scans/IMG_4912.jpg"),
        ("ред3", "Contracts/Договор_ред3.docx"),
        ("dogovor red3", "Contracts/Договор_ред3.docx"),
        ("Quarterly Planing Reveiw", "Inbox/Quarterly Planning Review.docx"),
    ])
    async def test_name_stream_finds_exact_partial_translit_and_typo(self, w, query, expected):
        """A record ranked in the top 61 of both streams outranks every single-stream record (2/(60+r) >
        1/61), so in a small drive a name match is in the top results whatever the card vectors say."""
        contracts, scans, inbox = (w.drive.add(n, folder=True) for n in ("Contracts", "Scans", "Inbox"))
        w.drive.add("Договор_ред1.docx", parent=contracts)
        w.drive.add("Договор_ред3.docx", parent=contracts)
        w.drive.add("IMG_4912.jpg", parent=scans)
        w.drive.add("IMG_5000.jpg", parent=scans)
        w.drive.add("Quarterly Planning Review.docx", parent=inbox)
        for i in range(20):
            w.drive.add(f"filler {chr(98 + i)}.bin", parent=inbox)   # shares no name key with any query
        await w.settle()
        result = await w.search(query)
        assert expected in paths(result)[:2]


    async def test_cards_carry_refs_kinds_and_distinct_nonces(self, w):
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            w.drive.add(name)
        await w.settle()
        result = await w.search("pdf")
        assert {c.ref for c in result.cards} == {f"drive:a1:{c.item_id}" for c in result.cards}
        assert len({c.nonce for c in result.cards}) == 3 and all(c.kind == "pdf" for c in result.cards)


    async def test_duplicates_collapse_by_content_hash(self, w):
        same = ContentIdentity("quickxor", "QX1", 10)
        a, b = w.drive.add("Contracts", folder=True), w.drive.add("Inbox", folder=True)
        w.drive.add("Lease.pdf", parent=a, content_identity=same, size_bytes=10)
        w.drive.add("Lease.pdf", parent=b, content_identity=same, size_bytes=10)
        await w.settle()
        result = await w.search("lease")
        lease = [c for c in result.cards if c.name == "Lease.pdf"]
        assert len(lease) == 1 and len(lease[0].also_at) == 1
        assert {lease[0].path, *lease[0].also_at} == {"Contracts/Lease.pdf", "Inbox/Lease.pdf"}


# --- read-time visibility (§5.2, §17.1 d) --------------------------------------------------------

class TestVisibility:
    async def test_repository_is_one_card_and_its_files_never_show(self, w):
        projects = w.drive.add("Projects", folder=True)
        repo_root = w.drive.add("alek-site", parent=projects, folder=True)
        w.drive.add(".git", parent=repo_root, folder=True)
        w.drive.add("alek-site-main.py", parent=repo_root)
        await w.settle()
        result = await w.search("alek-site")
        assert paths(result)[0] == "Projects/alek-site"
        assert not any(p.startswith("Projects/alek-site/") for p in paths(result))

    async def test_child_of_a_deleting_folder_is_hidden_at_once(self, w):
        folder = w.drive.add("Old", folder=True)
        w.drive.add("lease.pdf", parent=folder)
        await w.settle()
        assert "Old/lease.pdf" in paths(await w.search("lease"))
        w.set_state(folder, state=RecordState.DELETING)
        assert (await w.search("lease")).cards == ()   # neither the folder nor anything under it

    async def test_deleted_item_never_returns(self, w):
        item = w.drive.add("lease.pdf")
        await w.settle()
        w.drive.delete(item)
        await w.settle()
        assert (await w.search("lease")).cards == ()

    async def test_folder_rename_changes_paths_at_once(self, w):
        folder = w.drive.add("Contracts", folder=True)
        w.drive.add("lease.pdf", parent=folder)
        await w.settle()
        w.set_state(folder, name="Agreements")   # the record changed; no descendant was touched
        assert "Agreements/lease.pdf" in paths(await w.search("lease"))


# --- filters and folder scope --------------------------------------------------------------------

class TestFilters:
    async def test_filter_only_is_newest_first_without_knn_and_says_how_many(self, w):
        for i, day in enumerate(("2026-01-05", "2026-03-01", "2025-12-30")):
            w.drive.add(f"scan{i}.pdf", media=taken(day))
        w.drive.add("budget.xlsx", media=taken("2026-02-01"))
        await w.settle()
        calls = len(w.emb.calls)
        result = await w.search("", kind="pdf", date_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert paths(result) == ["scan1.pdf", "scan0.pdf"] and result.matched_total == 2
        assert not result.matched_at_least and len(w.emb.calls) == calls and w.repo.card_limits == []
        assert "2 files and folders match the filters" in render_results(result)


class TestFolderScope:
    async def _tree(self, w):
        contracts = w.drive.add("Contracts", folder=True)
        inbox = w.drive.add("Inbox", folder=True)
        w.drive.add("Lease agreement.pdf", parent=contracts)
        w.drive.add("Lease agreement.pdf", parent=inbox)
        work = w.drive.add("Work", folder=True)
        old = w.drive.add("Old", folder=True)
        w.drive.add("Agreements", parent=work, folder=True)
        w.drive.add("Agreements", parent=old, folder=True)
        alpha = w.drive.add("Alpha", parent=work, folder=True)
        deep = w.drive.add("a", parent=w.drive.add("b", parent=alpha, folder=True), folder=True)
        w.drive.add("x17.bin", parent=deep)
        await w.settle()
        return contracts

    async def test_by_path_keeps_only_the_subtree(self, w):
        await self._tree(w)
        result = await w.search("lease", folder="contracts")
        assert paths(result) == ["Contracts/Lease agreement.pdf"] and result.folder_path == "Contracts"
        assert "within the requested folder" in render_results(result)

    async def test_by_ref(self, w):
        contracts = await self._tree(w)
        assert paths(await w.search("lease", folder=f"drive:a1:{contracts}")) == ["Contracts/Lease agreement.pdf"]

    async def test_several_matches_are_candidates_not_a_guess(self, w):
        await self._tree(w)
        result = await w.search("lease", folder="Agreements")
        assert result.status == STATUS_AMBIGUOUS_FOLDER
        assert paths(result) == ["Old/Agreements", "Work/Agreements"]
        assert w.repo.name_limits[0][1] == "folder"

    async def test_unknown_folder(self, w):
        await self._tree(w)
        assert (await w.search("lease", folder="Taxes")).status == STATUS_FOLDER_NOT_FOUND

    async def test_weakly_named_file_deep_in_the_subtree_in_memory(self, w):
        await self._tree(w)
        result = await w.search("x17", folder="Work/Alpha")
        assert paths(result)[0] == "Work/Alpha/b/a/x17.bin" and w.repo.card_limits == []
        assert all(p.startswith("Work/Alpha/") for p in paths(result))


    async def test_filter_only_inside_a_folder(self, w):
        await self._tree(w)
        result = await w.search("", folder="Inbox")
        assert paths(result) == ["Inbox/Lease agreement.pdf"] and result.matched_total == 1


# --- scale: the name stream and the cover rule (RFC Δ17) ---------------

def names(result):
    return [c.name for c in result.cards]


@pytest.fixture(scope="module")
def camera_roll():
    """1 200 same-prefix photos (IMG_4912 is the 913th by item id, past any single 100-record page), a
    version family, 30 look-alike contracts and a lease. Built once: the tick over 1 300 items is the slow part."""
    import asyncio
    w = World(page_size=10_000)
    roll = w.drive.add("Camera roll", folder=True)
    for n in range(4000, 5200):
        w.drive.add(f"IMG_{n}.jpg", parent=roll, mime_type="image/jpeg")
    docs = w.drive.add("Contracts", folder=True)
    for v in (1, 2, 3):
        w.drive.add(f"Договор_ред{v}.docx", parent=docs)
    for i in range(30):
        w.drive.add(f"Договор поставки {i}.pdf", parent=docs)
    w.drive.add("Lease agreement.pdf", parent=docs)
    asyncio.run(w.settle())
    assert len(w.repo.records) > 1_200
    return w


@pytest.mark.slow
class TestScale:
    @pytest.mark.slow
    @pytest.mark.parametrize("query", ["IMG_4912", "4912", "img 4912", "IMG-4912 jpg"])
    async def test_exact_name_among_1200_same_prefix_files_is_first(self, camera_roll, query):
        assert names(await camera_roll.search(query))[0] == "IMG_4912.jpg"

    async def test_one_query_per_whole_token_key_plus_one_for_the_rest(self, camera_roll):
        # Name keys v2 (§25.2.4, P6 review M4; spec change): one fetch per concept with its own forms, then one per
        # trigram of a word concept (a number has none). A common word ("img", on all 1 200 photos) never shares a
        # capped page with a rare number, so nothing is left for a remainder call (RFC Δ17).
        w = camera_roll
        before = len(w.repo.name_keys)
        await w.search("IMG_4912")   # one id-like token: one number concept
        assert w.repo.name_keys[before:] == [("n:img4912",)]
        before = len(w.repo.name_keys)
        await w.search("IMG 4912")
        assert sorted(w.repo.name_keys[before:]) == sorted([("n:4912",), ("w:img", "x:imh"), ("t:img",), ("t:imh",)])
        assert names(await w.search("IMG 4912"))[0] == "IMG_4912.jpg"

    @pytest.mark.parametrize("query", ["ред3", "Договор ред3", "dogovor red3"])
    async def test_version_suffix_ranks_the_named_version_first(self, camera_roll, query):
        assert names(await camera_roll.search(query))[0] == "Договор_ред3.docx"


class TestFolderResolution:
    @pytest.mark.slow
    async def test_root_anchored_path_wins_among_150_same_named_folders(self, w):
        for i in range(150):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        target = w.drive.add("2025", parent=w.drive.add("Invoices", folder=True), folder=True)
        w.drive.add("march.pdf", parent=target)
        await w.settle()
        result = await w.search("march", folder="Invoices/2025")
        assert result.status == STATUS_OK and result.folder_path == "Invoices/2025"
        assert paths(result) == ["Invoices/2025/march.pdf"]

    async def test_many_matches_are_capped_candidates(self, w):
        for i in range(12):
            w.drive.add("2025", parent=w.drive.add(f"Project {i:02d}", folder=True), folder=True)
        await w.settle()
        result = await w.search("march", folder="2025")
        assert result.status == STATUS_AMBIGUOUS_FOLDER and len(result.cards) == 8
        assert paths(result)[0] == "Project 00/2025"

    async def test_nested_folder_by_its_own_name(self, w):
        deep = w.drive.add("Receipts", parent=w.drive.add("Finance", folder=True), folder=True)
        w.drive.add("taxi.pdf", parent=deep)
        await w.settle()
        assert paths(await w.search("taxi", folder="receipts")) == ["Finance/Receipts/taxi.pdf"]

    async def test_could_not_resolve_when_a_lookup_page_was_full(self, w, monkeypatch):
        for i in range(6):
            w.drive.add("2025", parent=w.drive.add(f"Project {i}", folder=True), folder=True)
        await w.settle()
        monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 3)   # the descent cannot read the top level
        monkeypatch.setattr(search_mod, "NAME_FETCH", 2)           # the name lookup page is full
        result = await w.search("march", folder="Taxes/2025")
        assert result.status == STATUS_FOLDER_NOT_FOUND and result.folder_lookup_truncated
        assert render_results(result).startswith("Could not resolve the folder")

    async def test_a_plain_miss_is_not_in_the_index(self, w):
        w.drive.add("Contracts", folder=True)
        await w.settle()
        result = await w.search("x", folder="Taxes")
        assert result.status == STATUS_FOLDER_NOT_FOUND and not result.folder_lookup_truncated


class TestKindFamilies:
    async def _sheets(self, w):
        for name, day in (("budget.xlsx", "2026-03-01"), ("plan.ods", "2026-02-01"), ("data.csv", "2026-01-01"),
                          ("old.xls", "2025-01-01"), ("report.pdf", "2026-04-01")):
            w.drive.add(name, media=taken(day))
        await w.settle()

    async def test_all_my_spreadsheets_filter_only_one_query_per_member(self, w):
        await self._sheets(w)
        result = await w.search("", kind="spreadsheet")
        assert paths(result) == ["budget.xlsx", "plan.ods", "data.csv", "old.xls"] and result.matched_total == 4
        assert sorted(w.repo.filter_kinds) == ["csv", "ods", "xls", "xlsx"]

    async def test_family_with_a_query_post_filters_both_streams(self, w):
        await self._sheets(w)
        result = await w.search("budget report", kind="spreadsheet")
        assert set(paths(result)) <= {"budget.xlsx", "plan.ods", "data.csv", "old.xls"}
        assert paths(result)[0] == "budget.xlsx" and w.repo.name_limits[-1][1] is None

    async def test_photos(self, w):
        w.drive.add("IMG_1.jpg", mime_type="image/jpeg")
        w.drive.add("notes.md")
        await w.settle()
        assert paths(await w.search("", kind="photo")) == ["IMG_1.jpg"]


# --- Cabinet status -------------------------------------------------------------------------------

class TestIndexingStatus:
    async def test_after_onboarding(self, w):
        w.drive.add("a.pdf")
        await w.settle()
        status = await w.search_svc.indexing_status("u")
        assert status.enabled and not status.building and status.counters == {"metadata_only": 1, "carded": 1}
        assert status.last_success_at == w.clock.now and status.last_error is None and not status.index_full

    async def test_before_any_tick(self, w):
        status = await w.search_svc.indexing_status("u")
        assert status.enabled and status.building and status.counters == {}

    async def test_turned_off_is_not_building(self, w):
        w.users.enabled["u"] = False
        status = await w.search_svc.indexing_status("u")
        assert not status.enabled and not status.building


def test_contract_signature():
    """The constructor named in the cross-part contract: (repo, embeddings, drive, connections, *, clock)."""
    import inspect
    params = inspect.signature(DriveSearchService.__init__).parameters
    assert list(params)[1:5] == ["repo", "embeddings", "drive", "connections"]
    assert params["clock"].kind is inspect.Parameter.KEYWORD_ONLY

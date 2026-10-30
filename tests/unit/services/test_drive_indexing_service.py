"""DriveIndexingService tick on the in-memory repository (USER_DRIVE_INDEXING_RFC §7, §17.4, §17.5)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index import CURRENT_VERSIONS, POLICY_VERSION, EpochPhase, ErrorCategory, Nudge, Reason, Scope
from src.domain.drive_index_params import ACCESS_LOSS_TTL, MAX_ATTEMPTS, TICK_START_BUDGET
from src.domain.user_drive import DriveConnection, DriveReauthRequired, DriveThrottled
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import ROOT, Clock, FakeEmbeddings, FakeQueue, FakeUsers
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE


# The version keys the harness's wired extractors add to the verified map (CORE-DELTA 5, §9, §12.3).
EXTRACTOR_VERSIONS = {"extractor:parser": 1, "extractor:reader": 1}


class World:
    def __init__(self, page_size=50):
        # wired as production wires it (indexer_main): a parser, a reader and triage; a file added without bytes
        # downloads empty, so its sniffed kind is `other` and it stays metadata-only as in Slice 1 (§8.1 step 4)
        self.drive = FakeContentDrive(page_size=page_size)
        self.repo = InMemoryDriveIndexRepository()
        self.emb = FakeEmbeddings()
        self.queue = FakeQueue()
        self.users = FakeUsers()
        self.clock = Clock()
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: "Rome (Roma), Italy", "test-space/8",
                                        extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                        triage=FakeTriage(), connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)

    async def tick(self, nudge=None):
        return await self.svc.tick(U_SCOPE, nudge)

    async def settle(self, max_ticks=50):
        """Run ticks while each one enqueues a continuation, as the drive queue would (delays elapse)."""
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick()
            continued = [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    async def settle_due(self, max_ticks=200, max_rounds=MAX_ATTEMPTS):
        """`settle`, then as the scheduler's fan-out would: let time run until every record is due again (an image
        without a rendition is retried with backoff, review B minor) and settle once more, until nothing is due.
        `max_rounds` bounds it: a retried item must converge within MAX_ATTEMPTS attempts (§8.2, §24.5)."""
        result = await self.settle(max_ticks)
        for _ in range(max_rounds):
            due = [r.due_at for (user, _), r in self.repo.records.items() if user == "u" and r.due_at is not None]
            if not due:
                return result
            self.clock.advance(max(1.0, (max(due) - self.clock.now).total_seconds()))
            result = await self.settle(max_ticks)
        return result

    def rec(self, item_id):
        return self.repo.records.get(("u", item_id))

    def carded(self):
        return {k[1] for k in self.repo.vectors}


@pytest.fixture
def w():
    return World()


# --- gate, lease, onboarding -------------------------------------------------------------------

async def test_disabled_without_state_writes_nothing(w):
    w.users.enabled["u"] = False
    w.drive.add("a.pdf")
    assert (await w.tick()).status == "disabled"
    assert w.repo.states == {} and w.repo.records == {}


async def test_onboarding_builds_records_cards_and_epoch_one(w):
    contracts = w.drive.add("Contracts", folder=True)
    doc = w.drive.add("Договор_ред3.docx", parent=contracts)
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.epoch == 1 and state.epoch_phase is EpochPhase.IDLE and state.initial_enumeration_done
    assert state.cursor.startswith("delta:") and state.root_item_id == ROOT and state.observed_as_of is not None
    assert w.rec(doc).reason is Reason.KIND_NOT_ADMITTED and w.rec(doc).has_card
    assert w.carded() == {contracts, doc}
    assert dict(state.applied_versions) == {**CURRENT_VERSIONS, **EXTRACTOR_VERSIONS}
    assert state.counters == {"metadata_only": 2, "carded": 2}


async def test_busy_lease_returns_busy_and_requeues_a_nudge(w):
    await w.repo.acquire_lease(U_SCOPE, "other", w.clock.now, w.clock.now + timedelta(minutes=31), create=True)
    assert (await w.tick()).status == "busy"
    assert (await w.tick(Nudge("m1", ("x",)))).status == "busy"
    keys = [t["key"] for t in w.queue.tasks]
    assert keys == ["u:cont:busy-other", "u:nudge:m1:r1"] and [t["delay"] for t in w.queue.tasks] == [60, 60]


async def test_nudge_is_read_before_delta(w):
    await w.settle()
    item = w.drive.add("note.md")
    w.drive.log.pop()   # delta has not caught up yet
    await w.tick(Nudge("m2", (item,)))
    assert w.rec(item) is not None and w.rec(item).has_card


# --- access loss, opt-out, drive switch (§7.6) -----------------------------------------------

async def test_loss_stamps_once_and_writes_nothing_else(w):
    w.drive.add("a.pdf")
    await w.settle()
    before = dict(w.repo.records)
    w.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
    assert (await w.tick()).status == "access_lost"
    first = (await w.repo.get_sync_state(U_SCOPE)).access_lost_at
    w.clock.advance(3600)
    await w.tick()
    assert (await w.repo.get_sync_state(U_SCOPE)).access_lost_at == first and w.repo.records == before


async def test_regain_within_ttl_continues_from_cursor(w):
    w.drive.add("a.pdf")
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    w.drive.connection_state = DriveConnection(connected=False)
    await w.tick()
    w.clock.advance(timedelta(days=3).total_seconds())
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d1")
    b = w.drive.add("b.pdf")
    await w.settle()
    after = await w.repo.get_sync_state(U_SCOPE)
    assert after.access_lost_at is None and after.epoch == state.epoch and w.rec(b) is not None


async def test_loss_past_ttl_purges(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=False)
    await w.tick()
    w.clock.advance(ACCESS_LOSS_TTL.total_seconds())
    await w.settle()
    assert w.repo.records == {} and w.repo.states == {}


async def test_opt_out_purges_records_and_state(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.users.enabled["u"] = False
    assert (await w.tick()).status == "purged"
    assert w.repo.records == {} and w.repo.states == {}


async def test_opt_out_mid_tick_drops_result_and_purges(w):
    for i in range(120):
        w.drive.add(f"f{i}.pdf")
    orig = w.repo.upsert_records
    calls = {"n": 0}

    async def flip_after_first(records, card_vectors=None):
        calls["n"] += 1
        await orig(records, card_vectors)
        w.users.enabled["u"] = False
        w.clock.advance(10)   # past the gate re-check interval

    w.repo.upsert_records = flip_after_first
    assert (await w.tick()).status == "disabled"
    assert calls["n"] == 1
    w.repo.upsert_records = orig
    await w.settle()
    assert w.repo.records == {} and w.repo.states == {}


async def test_drive_switch_purges_then_onboards(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
    assert (await w.tick()).status == "purged"
    assert w.repo.records == {} and [t["key"].split(":")[1] for t in w.queue.tasks][-1] == "cont"


# --- observation outcomes -------------------------------------------------------------------

async def test_tombstone_deletes_record_and_card(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    w.drive.delete(a)
    await w.settle()
    assert w.rec(a) is None and a not in w.carded()


async def test_folder_delete_cascades_post_order(w):
    top = w.drive.add("Top", folder=True)
    mid = w.drive.add("Mid", parent=top, folder=True)
    leaf = w.drive.add("leaf.pdf", parent=mid)
    await w.settle()
    w.drive.log.append(replace(w.drive.items[top], deleted=True))   # a provider that sends only the folder
    w.drive.items.clear()
    await w.settle()
    assert w.rec(top) is None and w.rec(mid) is None and w.rec(leaf) is None


async def test_folder_rename_reembeds_only_folder_card(w):
    top = w.drive.add("Old", folder=True)
    for i in range(5):
        w.drive.add(f"f{i}.pdf", parent=top)
    await w.settle()
    w.emb.calls.clear()
    w.drive.rename(top, "New")
    await w.settle()
    assert w.emb.calls == ["New | folder"]


async def test_reemitted_items_cost_no_write(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    written = []
    orig = w.repo.upsert_records

    async def spy(records, card_vectors=None):
        written.extend(r.item_id for r in records)
        await orig(records, card_vectors)

    w.repo.upsert_records = spy
    for _ in range(3):
        w.drive.reemit(a)
    await w.settle()
    assert written == []


async def test_git_after_siblings_same_round_one_card(w):
    site = w.drive.add("site", folder=True)
    for i in range(30):
        w.drive.add(f"m{i}.py", parent=site)
    w.drive.add(".git", parent=site, folder=True)
    await w.settle()
    assert w.carded() == {site}
    assert w.rec(site).scope is Scope.OPAQUE_ROOT
    assert sum(1 for (u, i), r in w.repo.records.items() if r.parent_id == site and not r.is_folder) == 0
    assert w.emb.calls == ["site | repository"]


@pytest.mark.slow
async def test_dense_folder_one_card_and_pdfs_stay_normal(w):
    dump = w.drive.add("mirror", folder=True)
    pdfs = w.drive.add("Invoices", folder=True)
    for i in range(510):
        w.drive.add(f"p{i}.bin", parent=dump)
        w.drive.add(f"inv{i}.pdf", parent=pdfs)
    await w.settle(max_ticks=200)
    assert w.rec(dump).reason is Reason.DENSE_TREE and w.rec(pdfs).reason is Reason.FOLDER
    assert sum(1 for r in w.repo.records.values() if r.parent_id == dump) == 0
    assert sum(1 for r in w.repo.records.values() if r.parent_id == pdfs) == 510


async def test_resync_starts_new_epoch_and_sweeps_unseen(w):
    a = w.drive.add("a.pdf")
    b = w.drive.add("b.pdf")
    await w.settle()
    w.drive.items.pop(b)            # gone, and the log never says so
    w.drive.resync_next = True
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.epoch == 2 and state.epoch_phase is EpochPhase.IDLE
    assert w.rec(a) is not None and w.rec(b) is None


# --- caps, budgets, errors ------------------------------------------------------------------

async def test_repo_drop_one_card_no_document_evicted(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 6)
    docs = [w.drive.add(f"d{i}.pdf") for i in range(5)]
    await w.settle()
    site = w.drive.add("site", folder=True)
    for i in range(40):
        w.drive.add(f"m{i}.py", parent=site)
    w.drive.add(".git", parent=site, folder=True)
    await w.settle()
    assert set(docs) <= w.carded()


async def test_card_cap_evicts_lower_rank_and_releases(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 3)
    codes = [w.drive.add(f"c{i}.py") for i in range(3)]
    await w.settle()
    doc = w.drive.add("contract.pdf")
    await w.settle()
    assert doc in w.carded() and len(w.carded()) == 3
    capped = [c for c in codes if w.rec(c).card_capped]
    assert len(capped) == 1 and w.rec(capped[0]).reason is Reason.KIND_NOT_ADMITTED
    w.drive.delete(doc)
    await w.settle()
    assert not w.rec(capped[0]).card_capped and w.rec(capped[0]).reason is Reason.KIND_NOT_ADMITTED
    assert capped[0] in w.carded()


async def test_budget_exhaustion_enqueues_exactly_one_continuation():
    w = World(page_size=5)
    for i in range(40):
        w.drive.add(f"f{i}.pdf")
    orig = w.drive.get_changes

    async def slow(user_id, cursor):
        w.clock.advance(TICK_START_BUDGET.total_seconds() / 3)
        return await orig(user_id, cursor)

    w.drive.get_changes = slow
    await w.tick()
    conts = [t for t in w.queue.tasks if ":cont:" in (t["key"] or "")]
    assert len(conts) == 1 and conts[0]["deadline"] == 1800


async def test_throttled_continues_with_delay(w):
    w.drive.raise_next = DriveThrottled(retry_after_s=45)
    assert (await w.tick()).status == "throttled"
    assert w.queue.tasks[-1]["delay"] == 45
    assert (await w.repo.get_sync_state(U_SCOPE)).last_error is ErrorCategory.THROTTLED


async def test_reauth_flags_once_and_never_purges(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.raise_next = DriveReauthRequired("invalid_grant")
    assert (await w.tick()).status == "access_lost"
    assert w.drive.flagged == 1 and len(w.repo.records) == 1
    assert (await w.repo.get_sync_state(U_SCOPE)).last_error is ErrorCategory.RECONNECT_NEEDED


async def test_version_bump_restamps_and_applies_after_clean_pass(w, monkeypatch):
    from src.domain import drive_index_planner as planner
    for i in range(3):
        w.drive.add(f"f{i}.pdf")
    await w.settle()
    bumped = POLICY_VERSION + 1
    monkeypatch.setattr(planner, "POLICY_VERSION", bumped)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**CURRENT_VERSIONS, "policy": bumped})
    await w.settle()
    assert all(r.policy_version == bumped for r in w.repo.records.values())
    assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions)["policy"] == bumped

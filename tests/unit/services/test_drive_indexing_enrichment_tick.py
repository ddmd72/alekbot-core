"""Search v2 P5 — the tick's enrichment machinery end to end (USER_DRIVE_INDEXING_RFC §25.4.5-§25.4.8, §25.8 with
D30): the migration gate, the order and shares, dollar budgets and the initial build, per-agent backoff and charges,
the keys pass, lease fencing and the per-file budget, a collapsed folder's README, triage v2 and the tick line."""
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_index as di
from src.domain.drive_enrichment import InitialBuild, JobState, ModelUsageMonth, PhotoDescription, PhotoObject
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply, TriageResultV2
from src.domain.drive_extraction import READER, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import KEYS_VERSION, MIGRATION_GATE_KEY, RecordState, Scope, TriageVerdict
from src.domain.drive_index_params import (
    LEASE_MARGIN, MAX_ATTEMPTS, MODEL_BACKOFF_CAP_S, READER_PAGE_USD_ESTIMATE, TICK_DEADLINE_S, TICK_START_BUDGET,
)
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import JPEG, admitted, prose  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld
from .drive_usage_helpers import initial_key

pytestmark = pytest.mark.usefixtures("admitted")
S = ModelReplyStatus


def doc_text(word: str = "Acme", n: int = 30) -> str:
    return f"{word} Rentals. " + prose(n)


@pytest.fixture
def w():
    return EnrichWorld()


# -- the migration gate (D30) -----------------------------------------------------------------------------------

async def test_a_fresh_onboarding_stamps_the_search_v2_versions(w):
    w.add_doc("a.md", doc_text())
    await w.settle()
    applied = w.repo.states["u"].applied_versions
    assert {k: applied[k] for k in ("keys", "profile", "passages")} == dict(di.SEARCH_V2_VERSIONS)


async def test_a_v1_index_does_no_tick_work_until_its_rebuild(w):
    doc = w.add_doc("a.md", doc_text())
    await w.settle()
    state = w.repo.states["u"]
    # a v1 index has neither the Search v2 keys nor `accuracy` (newer still; the gate key since Δ65)
    v1 = {k: v for k, v in state.applied_versions.items() if k not in {*di.SEARCH_V2_VERSIONS, "accuracy"}}
    w.repo.states["u"] = replace(state, applied_versions=v1)
    records = dict(w.repo.records)
    w.add_doc("b.md", doc_text("Brio"))
    queued = len(w.queue.tasks)
    result = await w.tick()
    assert result.status == "awaiting_migration"
    assert w.repo.records == records and len(w.queue.tasks) == queued   # nothing written, nothing continued
    assert MIGRATION_GATE_KEY not in w.repo.states["u"].applied_versions

    w.users.enabled["u"] = False   # the rebuild: a purge ...
    await w.settle()
    assert w.rec(doc) is None and "u" not in w.repo.states
    w.users.enabled["u"] = True    # ... then an onboarding under v2
    await w.settle()
    assert MIGRATION_GATE_KEY in w.repo.states["u"].applied_versions
    assert all(r.profile is not None for r in w.repo.records.values() if r.kind == "markdown")


# -- the order and the shares (§25.4.5) ---------------------------------------------------------------------------

async def test_content_keeps_to_its_share_and_enrichment_runs_in_the_same_tick(w):
    w.parser.before = lambda request: w.clock.advance(100)   # a slow parse
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(6)]
    result = await w.tick()
    indexed = [d for d in docs if w.rec(d).state is RecordState.INDEXED]
    assert len(indexed) == 3                                  # 0, 100, 200 s: the 60 % share ends at 288 s
    assert all(w.rec(d).profile is not None and w.rec(d).passages_state == JobState.CURRENT.value for d in indexed)
    assert result.status == "done" and any(":cont:" in (t["key"] or "") for t in w.queue.tasks)
    await w.settle()
    assert all(w.rec(d).profile_state == JobState.CURRENT.value for d in docs)


async def test_enrichment_keeps_at_least_its_share_when_content_fills_the_tick(w):
    w.profiler.before = lambda request: w.clock.advance(60)
    w.parser.before = lambda request: w.clock.advance(1)
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(12)]
    await w.tick()
    profiled = [d for d in docs if w.rec(d).profile is not None]
    # inline first-seen profiles take enrichment time, not the content share: content keeps going, and the start
    # budget still bounds the whole tick
    assert 0 < len(profiled) <= TICK_START_BUDGET.total_seconds() // 60 + 1


async def test_a_first_seen_file_is_profiled_in_its_content_run_and_carded_once(w):
    doc = w.add_doc("lease.md", doc_text())
    await w.tick()
    rec = w.rec(doc)
    assert rec.profile is not None and [t for t in w.emb.calls if t.startswith("lease.md | ")] == [
        svc_mod.card_text(rec)]


# -- dollars: the initial build first, the month's caps, parking (§25.4.5) ----------------------------------------

async def test_the_initial_build_pays_first_and_closes_when_the_build_settles(w):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    build = w.repo.initial[initial_key()]
    usage = w.repo.usage[("u", "2026-10")]
    spent = w.profiler.usd + w.passages.usd_per_chunk * w.rec(doc).chunk_count
    assert build.spent_usd == pytest.approx(spent) and build.opened_at is not None
    assert usage.profile_usd == pytest.approx(w.profiler.usd) and usage.calls >= 2
    assert build.closed_at is not None   # first onboarding done, nothing ready or parked: closed for good


async def test_spent_caps_park_the_jobs_until_the_month_turns(w):
    w.repo.initial[initial_key()] = InitialBuild(allowance_usd=40.0, spent_usd=40.0, closed_at=w.clock.now)
    w.repo.usage[("u", "2026-10")] = ModelUsageMonth(profile_usd=4.0, passage_usd=20.0)
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = w.rec(doc)
    assert (rec.profile_state, rec.passages_state) == (JobState.PARKED.value, JobState.PARKED.value)
    assert rec.profile_parked_month == "2026-10" and not rec.enrich_ready
    assert rec.enrich_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert w.profiler.requests == [] and w.passages.requests == []
    w.clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    await w.settle()
    rec = w.rec(doc)
    assert rec.profile_state == JobState.CURRENT.value and len(w.profiler.requests) == 1


async def test_the_readers_pages_are_paid_in_dollars(w):
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=doc_text(),
                                                      model_calls=1, model_cost=0.0012, pages_sent=2,
                                                      pages_total=2, extracted_by=f"{READER}@1")
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.add_doc("scan.pdf", "x")
    await w.settle()
    assert w.repo.usage[("u", "2026-10")].reader_usd == pytest.approx(0.0012)
    assert w.reader.requests and w.reader.requests[0].max_model_pages >= int(4.0 / READER_PAGE_USD_ESTIMATE)


async def test_a_spent_reader_month_defers_the_scan_to_the_next_month(w):
    w.repo.initial[initial_key()] = InitialBuild(allowance_usd=40.0, spent_usd=40.0, closed_at=w.clock.now)
    w.repo.usage[("u", "2026-10")] = ModelUsageMonth(reader_usd=4.0)
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    doc = w.add_doc("scan.pdf", "x")
    await w.settle()
    rec = w.rec(doc)
    assert w.reader.requests == [] and rec.state is RecordState.PENDING
    assert rec.defer_until == datetime(2026, 11, 1, tzinfo=timezone.utc)


# -- failures and charges (§25.4.6) ------------------------------------------------------------------------------

async def test_an_outage_backs_off_only_its_agent_type_and_charges_nothing(w):
    w.profiler.script = [S.UNAVAILABLE]
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = w.rec(doc)
    state = w.repo.states["u"]
    assert rec.state is RecordState.INDEXED and rec.profile is None and rec.profile_attempts == 0
    assert rec.profile_state == JobState.READY.value                   # not a job state: the job stays ready
    assert "drive_profiler" in state.model_backoff_by_agent and "drive_passages" not in state.model_backoff_by_agent
    assert rec.passages_state == JobState.CURRENT.value                 # passages went on
    w.clock.advance(MODEL_BACKOFF_CAP_S)
    await w.settle()
    rec = w.rec(doc)
    assert rec.profile is not None and "drive_profiler" not in w.repo.states["u"].model_backoff_by_agent


async def test_failed_is_charged_and_backs_off_and_holds_at_max_attempts(w):
    w.profiler.script = [S.FAILED] * MAX_ATTEMPTS
    doc = w.add_doc("lease.md", doc_text())
    for _ in range(MAX_ATTEMPTS):
        await w.settle()
        w.clock.advance(MODEL_BACKOFF_CAP_S + 1)
    rec = w.rec(doc)
    assert (rec.profile_hold, rec.profile_attempts) == ("failed", MAX_ATTEMPTS)
    assert rec.profile_state == JobState.HELD.value and len(w.profiler.requests) == MAX_ATTEMPTS
    assert rec.state is RecordState.INDEXED                              # never a content failure


async def test_invalid_is_charged_without_a_backoff(w):
    w.profiler.script = [S.INVALID]
    doc = w.add_doc("lease.md", doc_text())
    await w.tick()
    rec = w.rec(doc)
    assert rec.profile_attempts == 1 and "drive_profiler" not in w.repo.states["u"].model_backoff_by_agent
    await w.settle()
    assert w.rec(doc).profile is not None and len(w.profiler.requests) == 2


async def test_a_refusal_holds_until_the_input_changes(w):
    w.profiler.script = [S.REFUSED]
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = w.rec(doc)
    assert (rec.profile_state, rec.profile_hold) == (JobState.HELD.value, "refused")
    w.drive.edit(doc, doc_text("Brio").encode(), modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(doc).profile is not None and len(w.profiler.requests) == 2


async def test_a_refused_chunk_gets_no_hints_and_never_holds_the_file(w):
    w.passages.refuse = {0}
    doc = w.add_doc("lease.md", doc_text(n=80))
    await w.settle()
    rec = w.rec(doc)
    assert rec.chunk_count > 1 and rec.passages_state == JobState.CURRENT.value and rec.passages_hold is None
    assert [h.index for h in w.hints(doc)] == list(range(1, rec.chunk_count))


async def test_a_failed_passage_call_backs_off_and_resumes(w):
    w.passages.script = [S.FAILED]
    doc = w.add_doc("lease.md", doc_text(n=80))
    await w.settle()
    rec = w.rec(doc)
    assert rec.passages_attempts == 1 and "drive_passages" in w.repo.states["u"].model_backoff_by_agent
    w.clock.advance(MODEL_BACKOFF_CAP_S + 1)
    await w.settle()
    rec = w.rec(doc)
    assert rec.passages_state == JobState.CURRENT.value
    assert [h.index for h in w.hints(doc)] == list(range(rec.chunk_count))


async def test_the_fallbacks_dollars_are_kept_per_day(w):
    w.profiler.fallback_share = 1.0
    w.add_doc("lease.md", doc_text())
    await w.settle()
    usage = w.repo.usage[("u", "2026-10")]
    assert usage.fallback_usd == pytest.approx(w.profiler.usd)
    total, fallback = usage.daily["2026-10-11"]
    assert fallback == pytest.approx(w.profiler.usd) and total > fallback


# -- the keys pass (§25.4.8) -------------------------------------------------------------------------------------

async def test_a_keys_bump_re_keys_into_a_new_generation_without_model_calls(w, monkeypatch):
    doc = w.add_doc("lease.md", doc_text(n=60))
    await w.settle()
    before = w.rec(doc)
    hints_before = w.hints(doc)
    profiles, passages, embeds = len(w.profiler.requests), len(w.passages.requests), len(w.emb.calls)
    bumped = KEYS_VERSION + 1
    from src.domain import drive_enrich_plan, drive_index_planner
    for module in (drive_enrich_plan, drive_index_planner, svc_mod):
        monkeypatch.setattr(module, "KEYS_VERSION", bumped)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**di.CURRENT_VERSIONS, "keys": bumped})
    await w.settle()
    rec = w.rec(doc)
    assert rec.keys_version == bumped and not rec.keys_pending
    assert rec.indexed_generation == before.indexed_generation + 1 and w.generations(doc) == [rec.indexed_generation]
    assert all(c.keys_version == bumped for c in w.readable(doc))
    assert [h.index for h in w.hints(doc)] == [h.index for h in hints_before]
    assert w.hint_generations(doc) == [rec.indexed_generation] and rec.passages_generation == rec.indexed_generation
    assert (len(w.profiler.requests), len(w.passages.requests), len(w.emb.calls)) == (profiles, passages, embeds)
    assert w.repo.states["u"].applied_versions["keys"] == bumped


async def test_a_profile_version_bump_re_profiles_from_stored_text_and_applies_once_settled(w, monkeypatch):
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(3)]
    await w.settle()
    downloads = len(w.drive.downloads)
    bumped = di.PROFILE_VERSION + 1
    from src.domain import drive_enrich_plan, drive_profile
    for module in (drive_enrich_plan, drive_profile):
        monkeypatch.setattr(module, "PROFILE_VERSION", bumped)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**di.CURRENT_VERSIONS, "profile": bumped})
    await w.settle()
    assert all(w.rec(d).profile_version == bumped for d in docs)   # inside the cooldown: a bump is not deferred
    assert len(w.profiler.requests) == 6 and len(w.drive.downloads) == downloads   # stored text, no re-read
    assert w.repo.states["u"].applied_versions["profile"] == bumped


async def test_the_emergency_switch_stops_every_upgrade_pass(monkeypatch):
    w = EnrichWorld(upgrade_pass=False)
    doc = w.add_doc("lease.md", doc_text(n=60))
    await w.settle()
    bumped = KEYS_VERSION + 1
    from src.domain import drive_enrich_plan, drive_index_planner
    for module in (drive_enrich_plan, drive_index_planner, svc_mod):
        monkeypatch.setattr(module, "KEYS_VERSION", bumped)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**di.CURRENT_VERSIONS, "keys": bumped})
    await w.settle()
    assert w.rec(doc).keys_version == KEYS_VERSION and w.repo.states["u"].applied_versions["keys"] == KEYS_VERSION


# -- the lease and the per-file budget ---------------------------------------------------------------------------

async def test_enrichment_writes_nothing_once_the_lease_is_ending(w):
    doc = w.add_doc("lease.md", doc_text())
    w.profiler.before = lambda request: w.clock.advance(TICK_DEADLINE_S)   # past lease_until - LEASE_MARGIN
    result = await w.tick()
    assert result.status == "lease_lost" and w.rec(doc).profile is None
    assert LEASE_MARGIN.total_seconds() > 0


async def test_a_file_over_its_enrichment_budget_resumes_from_saved_progress(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "ENRICH_FILE_BUDGET_S", 0.2)
    w.passages.delay_s, w.passages.fast_calls = 0.5, 2     # the first wave answers at once, the second never in time
    doc = w.add_doc("lease.md", doc_text(n=150))
    await w.tick()
    rec = w.rec(doc)
    assert rec.chunk_count > 4 and rec.passages_next_index == 4 and len(w.hints(doc)) == 4
    w.passages.delay_s = 0.0
    await w.settle()
    rec = w.rec(doc)
    assert rec.passages_state == JobState.CURRENT.value
    assert [h.index for h in w.hints(doc)] == list(range(rec.chunk_count))
    sent = [i for r in w.passages.requests for i, _ in r.chunks]
    assert sorted(set(sent)) == list(range(rec.chunk_count))


# -- a collapsed folder's README (§25.3) ---------------------------------------------------------------------------

async def test_a_hidden_folder_is_profiled_from_its_readme(w):
    folder = w.drive.add(".tools", folder=True)
    w.add_doc("README.md", "# Invoice tool\n\nAcme converts invoices to CSV files every month.", parent=folder)
    w.add_doc("main.py", "print('x')", parent=folder)
    await w.settle()
    rec = w.rec(folder)
    assert rec.scope is Scope.OPAQUE_ROOT and "Invoice tool" in rec.readme_excerpt and "main.py" in rec.readme_excerpt
    assert rec.profile is not None and rec.profile_state == JobState.CURRENT.value
    assert w.profiler.requests[-1].kind == "folder" and w.profiler.requests[-1].input.child_names
    assert "README.md" not in w.passages.requests and rec.passages_state == JobState.NONE.value


# -- triage v2 (§25.4.3) -----------------------------------------------------------------------------------------

async def test_triage_writes_the_photo_fields_and_its_dollars(w):
    photo = PhotoDescription(description="a fountain at night", objects=(PhotoObject("fountain", ("fontana",)),),
                             scene="city square", visible_text="BAR 24")

    async def triage(user_id, account_id, images):
        return TriageReply(status=S.OK, model="m", usd=0.002, fallback_usd=0.0, model_calls=1,
                           results=tuple(TriageResultV2(i.item_id, TriageVerdict.PHOTO, photo) for i in images))

    w.triage.triage = triage
    img = w.add_image("IMG_1.jpg", camera=True)
    await w.settle()
    rec = w.rec(img)
    assert (rec.photo_description, rec.photo_scene, rec.visible_text, rec.caption) == (
        "a fountain at night", "city square", "BAR 24", "")
    assert "pe:fontana" in rec.about_keys and "pw:fountain" in rec.about_keys
    assert w.repo.usage[("u", "2026-10")].triage_usd == pytest.approx(0.002)
    assert "a fountain at night | city square | fountain" in svc_mod.card_text(rec)


async def test_a_refused_image_is_a_photograph_without_a_description(w):
    async def triage(user_id, account_id, images):
        return TriageReply(status=S.OK, model="m", usd=0.0, results=(),
                           refused_item_ids=tuple(i.item_id for i in images))

    w.triage.triage = triage
    img = w.add_image("IMG_2.jpg")
    await w.settle()
    rec = w.rec(img)
    assert rec.triage_verdict is TriageVerdict.PHOTO and rec.photo_description == ""


# -- telemetry (§25.7) -------------------------------------------------------------------------------------------

async def test_the_tick_line_counts_profiles_passages_and_dollars(w, caplog):
    w.add_doc("lease.md", doc_text())
    with caplog.at_level(logging.INFO):
        await w.settle()
    lines = [r.getMessage() for r in caplog.records if "[DriveIndex] tick user=" in r.getMessage()]
    joined = " ".join(lines)
    assert "'profiles': 1" in joined and "'passage_chunks':" in joined
    assert "profile_usd=$" in joined and "passage_usd=$" in joined and "initial_build_usd=$" in joined
    assert any("rebuild progress" in r.getMessage() for r in caplog.records)
    counters = w.repo.states["u"].counters
    assert counters.get("profile_current") == 1 and counters.get("passages_current") == 1
    assert w.repo.states["u"].described_count == 1 and w.repo.states["u"].described_grew_at is not None

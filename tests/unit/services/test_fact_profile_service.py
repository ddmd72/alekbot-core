"""FactProfileService (fact profile RFC §4.2, §4.4): the real FirestoreFactRepository over the evaluating fake, a scripted
profiler, a recording embedder, a settable clock and a tiny atlas. Examples come from the invented universe."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import FactDomain, FactState
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.fact_profile_fingerprint import (
    ABANDON_AFTER_FAILURES, profile_derivation, profile_fingerprint, registry_digest,
)
from src.domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from src.domain.fact_profile_run import FactProfileRun
from src.domain.memory_write_lease import LEASE_MARGIN
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.ports.embedding_service import EmbeddingService
from src.ports.fact_profiler_port import FactProfilerPort
from src.services.fact_profile_service import FactProfileService
from tests.support.fact_kit import ACC, OTHER, T0 as SEEDED_AT, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, embed, make_env_config

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
BUDGET = 120.0
ATLAS_DIGEST = "atlas-1"
CAR = "Alex's car insurance with Example Mutual Insurance runs 2026-04-01 to 2027-03-31"
TRIP = "Alex booked Albergo Immaginario in Rome, booking RM-55120"
BIRTHDAY = "Clara Moreira was born on 14 July 1982"
PASSPORT = "Alex's passport expires on 2031-03-04"
POLLEN = "Alex has a pollen allergy"
UNION = "Alex pays 12 EUR/month for the trade-union membership at Acme"
DIRECTIVE = "Alex talks to the bot in English"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def answer(**over: Any) -> ProfileAnswer:
    fields: Dict[str, Any] = dict(
        label_words=["vehicle cover"], time_role="timeless", time_points=[], places=[], people=[], tax_or_fine="none",
        user_identity=False, pet=False, state_or_law=False, about_person="none", subject="transport", money_flow=False,
        use_areas=[], kinds=[], sensitivity="ordinary")
    fields.update(over)
    return ProfileAnswer.model_validate(fields)


def ok(a: Optional[ProfileAnswer] = None, usd: float = 0.004, model: str = "model-x") -> FactProfilerReply:
    return FactProfilerReply(ProfilerStatus.OK, a or answer(), model, usd, 1)


def status_reply(status: ProfilerStatus, usd: float = 0.0) -> FactProfilerReply:
    return FactProfilerReply(status, None, "model-x", usd, 1)


class Profiler(FactProfilerPort):
    """Scripted replies by fact text (default: an ok answer); records the calls and the most calls in flight."""

    def __init__(self) -> None:
        self.replies: Dict[str, FactProfilerReply] = {}
        self.calls: List[str] = []
        self.accounts: List[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold: Optional[asyncio.Event] = None
        self.on_call: Any = None

    async def profile(self, account_id, text, metadata, people=()):
        self.calls.append(text)
        self.accounts.append(account_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self.hold is not None:
                await self.hold.wait()
            if self.on_call is not None:
                self.on_call(text)
            return self.replies.get(text) or ok()
        finally:
            self.in_flight -= 1


class Embedder(EmbeddingService):
    def __init__(self) -> None:
        self.batches: List[List[str]] = []

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        raise AssertionError("the step embeds in one batch")

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.batches.append(list(texts))
        return [embed(t) for t in texts]


class CountingRepo(FirestoreFactRepository):
    refreshes: List[str]

    async def refresh_biographical_context_cache(self, owner_id, facts_limit=None, principles_limit=None):
        self.refreshes.append(owner_id)


ROME = PlaceRow(41.9028, 12.4964, "Rome", "Roma", "Italy", 2_800_000, geonameid="1")


class World:
    def __init__(self, **over: Any) -> None:
        self.store = FactStore(indexes=INDEXES_FILE)
        self.repo = CountingRepo(self.store.db, make_env_config(), embedding_service=self.store.embedder)
        self.repo.refreshes = []
        self.profiler = Profiler()
        self.embedder = Embedder()
        self.clock = Clock()
        self.atlas_calls = 0
        kwargs: Dict[str, Any] = dict(max_per_run=20, call_budget_s=BUDGET)
        kwargs.update(over)
        self.service = FactProfileService(
            repo=self.repo, profiler=self.profiler, embeddings=self.embedder, atlas=self._atlas,
            concurrency=4, clock=self.clock, **kwargs)

    async def _atlas(self):
        self.atlas_calls += 1
        return PlaceNames([ROME]), ATLAS_DIGEST

    @property
    def until(self) -> datetime:
        return self.clock.now + timedelta(seconds=3600) + LEASE_MARGIN

    @property
    def docs(self) -> dict:
        return self.store.docs

    def derivation(self, directive: bool = False) -> str:
        # The recording year of every seeded fact: make_fact's created_at (SEEDED_AT); reported_date is the wall
        # clock at seeding, later, so the earlier of the two is created_at. The step's clock (T0) is not its source.
        return profile_derivation(registry_digest(), ATLAS_DIGEST, directive, SEEDED_AT.year)

    async def seed(self, text: str, **over: Any):
        fact = make_fact(text, **over)
        await self.repo.add_fact(fact)
        return fact

    async def run(self, account: str = ACC) -> FactProfileRun:
        return await self.service.run(account, self.until)

    async def profile_everything(self) -> None:
        run = await self.run()
        assert not run.error


@pytest.fixture
def world():
    return World()


def fid(text: str) -> str:
    return f"f-{text}"


# ------------------------------------------------------------------------------------------------ scan and counts

async def test_scan_counts_each_status_and_what_a_run_would_call(world):
    await world.seed(CAR)                                                   # missing
    stale = await world.seed(TRIP)
    await world.profile_everything()
    world.docs[fid(TRIP)]["text"] = TRIP + ", two nights"                   # stale_input
    world.docs[fid(CAR)]["profile_derivation"] = "old"                      # stale_derivation with an answer
    await world.seed(PASSPORT)                                              # missing
    await world.seed(POLLEN, profile_failures=ABANDON_AFTER_FAILURES,
                     profile_failed_fingerprint=profile_fingerprint(POLLEN, None))   # abandoned
    assert stale.id == fid(TRIP)

    scan = await world.service.scan(ACC)

    assert (scan.scanned, scan.missing, scan.stale_input, scan.stale_derivation, scan.abandoned, scan.fresh) == (
        4, 1, 1, 1, 1, 0)
    assert scan.to_call == 2                                               # missing + stale_input; the re-derivation is free


async def test_scan_writes_nothing_and_a_system_account_is_not_read(world):
    await world.seed(CAR)
    before = dict(world.docs[fid(CAR)])

    await world.service.scan(ACC)
    system = await world.service.run("SYSTEM", world.until)

    assert world.docs[fid(CAR)] == before and world.profiler.calls == []
    assert system == FactProfileRun()
    assert await world.service.scan("SYSTEM") == FactProfileRun().scan


async def test_a_system_account_run_reads_nothing(world, monkeypatch):
    async def forbidden(account_id):
        raise AssertionError("read")
    monkeypatch.setattr(world.repo, "list_profile_states", forbidden)

    run = await world.service.run("SYSTEM", world.until)

    assert run == FactProfileRun() and not run.error


async def test_only_the_accounts_own_facts_are_profiled(world):
    await world.seed(CAR)
    await world.seed(TRIP, account=OTHER)

    run = await world.run()

    assert run.scan.scanned == 1 and world.profiler.calls == [CAR]
    assert world.docs[fid(TRIP)].get("profile_fingerprint") is None


# ------------------------------------------------------------------------------------------------ free work

async def test_a_stale_derivation_is_rederived_from_its_stored_answer_without_a_call(world):
    await world.seed(CAR)
    await world.profile_everything()
    world.profiler.calls.clear()
    world.docs[fid(CAR)]["profile_derivation"] = "old"
    world.docs[fid(CAR)]["label_words"] = ["stale label"]
    before = world.docs[fid(CAR)]["profile_fingerprint"]

    run = await world.run()

    assert world.profiler.calls == [] and (run.rederived, run.written, run.called) == (1, 1, 0)
    doc = world.docs[fid(CAR)]
    assert doc["profile_derivation"] == world.derivation() and doc["label_words"] == ["vehicle cover"]
    assert doc["profile_fingerprint"] == before


async def test_a_directive_gets_its_fixed_profile_without_a_call(world):
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)

    run = await world.run()

    assert world.profiler.calls == [] and (run.directives, run.written, run.called) == (1, 1, 0)
    doc = world.docs[fid(DIRECTIVE)]
    assert doc["primary_domain"] == "agent_directives" and doc["domain_keys"] == ["agent_directives"]
    assert doc["profile_derivation"] == world.derivation(True) and doc["profile_model"] is None
    assert doc["label_vector"] is None and doc["profile_fingerprint"] == profile_fingerprint(DIRECTIVE, None)
    assert world.embedder.batches == []


async def test_a_directive_is_fresh_the_second_time(world):
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)
    await world.run()

    run = await world.run()

    assert (run.scan.fresh, run.written, run.directives) == (1, 0, 0)


async def test_a_stale_directive_derivation_writes_the_rederivation_fields_only(world):
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)
    await world.run()
    world.docs[fid(DIRECTIVE)]["profile_derivation"] = "old"
    world.docs[fid(DIRECTIVE)]["profiled_at"] = None

    run = await world.run()

    assert run.directives == 1 and run.rederived == 0 and world.profiler.calls == []
    doc = world.docs[fid(DIRECTIVE)]
    assert doc["profile_derivation"] == world.derivation(True) and doc["profiled_at"] is None


async def test_a_former_directive_with_no_stored_answer_is_called(world):
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)
    await world.run()
    world.docs[fid(DIRECTIVE)]["domain"] = FactDomain.HEALTH.value

    run = await world.run()

    assert world.profiler.calls == [DIRECTIVE] and run.called == 1 and run.scan.stale_derivation == 1
    assert world.docs[fid(DIRECTIVE)]["profile_derivation"] == world.derivation(False)


async def test_an_unreadable_stored_answer_means_a_call(world):
    await world.seed(CAR)
    await world.profile_everything()
    world.profiler.calls.clear()
    world.docs[fid(CAR)]["profile_derivation"] = "old"
    world.docs[fid(CAR)]["profile_answer"] = {"subject": "no-such-subject"}

    run = await world.run()

    assert world.profiler.calls == [CAR] and run.called == 1 and run.rederived == 0


async def test_an_abandoned_fact_is_skipped(world):
    await world.seed(POLLEN, profile_failures=ABANDON_AFTER_FAILURES,
                     profile_failed_fingerprint=profile_fingerprint(POLLEN, None))

    run = await world.run()

    assert world.profiler.calls == [] and run.scan.abandoned == 1 and run.written == 0 and run.refreshed is False


# ------------------------------------------------------------------------------------------------ calls: order, caps

async def test_the_newest_facts_are_called_first_and_the_cap_leaves_the_rest_deferred():
    world = World(max_per_run=2)
    for age, text in enumerate((CAR, TRIP, BIRTHDAY, PASSPORT)):
        await world.seed(text, age_days=age)

    run = await world.run()

    assert world.profiler.calls == [PASSPORT, BIRTHDAY]
    assert (run.called, run.written, run.deferred, run.scan.to_call) == (2, 2, 2, 4)


async def test_the_default_cap_of_twenty_and_no_cap():
    capped, unlimited = World(max_per_run=20), World(max_per_run=None)
    for w in (capped, unlimited):
        for n in range(22):
            await w.seed(f"Alex keeps record number {n}", age_days=n)

    assert (await capped.run()).called == 20
    assert (await unlimited.run()).called == 22


async def test_a_cap_of_zero_rederives_only():
    world = World(max_per_run=0)
    await world.seed(CAR)
    await world.seed(TRIP)
    world.docs[fid(CAR)].update(profile_fingerprint=profile_fingerprint(CAR, None), profile_derivation="old",
                                profile_answer=answer().stored())

    run = await world.run()

    assert world.profiler.calls == [] and (run.called, run.rederived, run.deferred) == (0, 1, 1)


async def test_never_more_than_four_calls_are_in_flight(world):
    for n in range(10):
        await world.seed(f"Alex keeps record number {n}", age_days=n)

    run = await world.run()

    assert run.called == 10 and world.profiler.max_in_flight == 4


async def test_the_deadline_stops_new_calls_and_counts_them_deferred():
    world = World(call_budget_s=BUDGET)
    for n in range(6):
        await world.seed(f"Alex keeps record number {n}", age_days=n)
    # The first wave of four starts; then the clock moves so that less than the call budget is left.
    ticked = []
    world.profiler.on_call = lambda text: (world.clock.advance(520), ticked.append(1)) if not ticked else None
    until = world.clock.now + timedelta(seconds=BUDGET + 60 + 500)

    run = await world.service.run(ACC, until)

    assert run.called == 4 and run.deferred == 2


async def test_no_call_starts_when_the_budget_is_not_left(world):
    await world.seed(CAR)

    run = await world.service.run(ACC, world.clock.now + LEASE_MARGIN + timedelta(seconds=BUDGET))

    assert world.profiler.calls == [] and (run.called, run.deferred, run.written) == (0, 1, 0)


async def test_an_unavailable_reply_ends_the_step_and_nothing_is_written_for_it():
    world = World()
    for age, text in enumerate((CAR, TRIP, BIRTHDAY, PASSPORT, POLLEN, UNION)):
        await world.seed(text, age_days=age)
    world.profiler.replies[POLLEN] = status_reply(ProfilerStatus.UNAVAILABLE)

    run = await world.run()

    assert run.outage and run.failed == 0
    assert world.docs[fid(POLLEN)].get("profile_failures") is None and world.docs[fid(POLLEN)]["profile_fingerprint"] is None
    assert run.called + run.deferred == 6


async def test_after_an_unavailable_reply_the_in_flight_calls_finish_and_no_new_one_starts():
    world = World()
    for n in range(8):
        await world.seed(f"Alex keeps record number {n}", age_days=n)
    first = "Alex keeps record number 7"                   # the newest is called first
    world.profiler.replies[first] = status_reply(ProfilerStatus.UNAVAILABLE)

    run = await world.run()

    assert run.outage and run.called == 4 and run.deferred == 4 and run.written == 3


# ------------------------------------------------------------------------------------------------ failures

@pytest.mark.parametrize("reply", [status_reply(ProfilerStatus.INVALID, 0.002), status_reply(ProfilerStatus.REFUSED, 0.001)])
async def test_an_invalid_or_refused_reply_writes_only_the_failure_fields(world, reply):
    await world.seed(CAR)
    world.profiler.replies[CAR] = reply

    run = await world.run()

    assert (run.failed, run.written, run.refreshed) == (1, 0, False) and run.usd == pytest.approx(reply.usd)
    doc = world.docs[fid(CAR)]
    assert doc["profile_failures"] == 1 and doc["profile_failed_fingerprint"] == profile_fingerprint(CAR, None)
    assert doc["profile_fingerprint"] is None and doc["label_words"] is None and doc["primary_domain"] is None


async def test_an_intimate_answer_without_labels_writes_only_the_failure(world):
    await world.seed(UNION)
    world.profiler.replies[UNION] = ok(answer(sensitivity="intimate", label_words=[]))

    run = await world.run()

    assert (run.failed, run.written) == (1, 0) and run.drops.get("intimate_without_labels") == 1
    assert world.docs[fid(UNION)]["profile_failures"] == 1 and world.docs[fid(UNION)]["profile_fingerprint"] is None


async def test_the_third_failure_on_one_fingerprint_abandons_once_and_the_next_scan_skips_it(world, caplog):
    await world.seed(CAR)
    world.profiler.replies[CAR] = status_reply(ProfilerStatus.INVALID)
    runs = []
    with caplog.at_level(logging.INFO):
        for _ in range(ABANDON_AFTER_FAILURES):
            runs.append(await world.run())
    after = await world.run()

    assert [r.newly_abandoned for r in runs] == [0, 0, 1] and [r.failed for r in runs] == [1, 1, 1]
    outcomes = [rec.getMessage() for rec in caplog.records if "outcome=" in rec.getMessage()]
    assert [o.split("outcome=")[1].split()[0] for o in outcomes] == ["failed", "failed", "abandoned"]
    assert after.scan.abandoned == 1 and after.called == 0 and len(world.profiler.calls) == ABANDON_AFTER_FAILURES


async def test_a_new_fingerprint_restarts_the_count(world):
    await world.seed(CAR)
    world.profiler.replies[CAR] = status_reply(ProfilerStatus.INVALID)
    await world.run()
    await world.run()
    assert world.docs[fid(CAR)]["profile_failures"] == 2

    world.docs[fid(CAR)]["text"] = CAR + ", renewed"
    world.profiler.replies[CAR + ", renewed"] = status_reply(ProfilerStatus.INVALID)
    await world.run()

    assert world.docs[fid(CAR)]["profile_failures"] == 1
    assert world.docs[fid(CAR)]["profile_failed_fingerprint"] == profile_fingerprint(CAR + ", renewed", None)


async def test_a_success_clears_an_earlier_failure(world):
    await world.seed(CAR)
    world.profiler.replies[CAR] = status_reply(ProfilerStatus.REFUSED)
    await world.run()
    world.profiler.replies[CAR] = ok()

    await world.run()

    doc = world.docs[fid(CAR)]
    assert doc["profile_failures"] is None and doc["profile_failed_fingerprint"] is None
    assert doc["profile_fingerprint"] == profile_fingerprint(CAR, None)


# ------------------------------------------------------------------------------------------------ what is written

SUCCESS_FIELDS = {
    "label_words", "label_vector", "time_role", "time_points", "place_keys", "places_unresolved", "people",
    "primary_domain", "domain_keys", "user_identity", "kinds", "sensitivity", "profile_answer", "profile_drops",
    "profile_fingerprint", "profile_derivation", "profile_model", "profiled_at", "profile_failures",
    "profile_failed_fingerprint", "profile_review_needed", "profile_review_items",
}
REDERIVE_FIELDS = SUCCESS_FIELDS - {"profile_fingerprint", "profile_model", "profiled_at", "profile_failures",
                                    "profile_failed_fingerprint"}
FAILURE_FIELDS = {"profile_failures", "profile_failed_fingerprint"}


class Spy:
    def __init__(self, world: World) -> None:
        self.writes: List[Dict[str, Any]] = []
        real = world.repo.write_fact_profile

        async def write(fact_id, fields, expected_fingerprint):
            self.writes.append({"id": fact_id, "fields": dict(fields), "expected": expected_fingerprint})
            return await real(fact_id, fields, expected_fingerprint)

        world.repo.write_fact_profile = write


async def test_a_success_writes_exactly_the_success_fields_under_the_scanned_fingerprint(world):
    spy = Spy(world)
    await world.seed(CAR)
    world.profiler.replies[CAR] = ok(model="model-y")

    await world.run()

    (write,) = spy.writes
    assert set(write["fields"]) == SUCCESS_FIELDS and write["expected"] is None
    doc = world.docs[fid(CAR)]
    assert doc["profile_model"] == "model-y" and doc["profiled_at"] == T0
    assert doc["profile_fingerprint"] == profile_fingerprint(CAR, None) and doc["profile_derivation"] == world.derivation()
    assert doc["primary_domain"] == "transport" and doc["label_words"] == ["vehicle cover"]


async def test_a_rederivation_writes_exactly_its_fields_under_the_stored_fingerprint(world):
    await world.seed(CAR)
    await world.profile_everything()
    world.docs[fid(CAR)]["profile_derivation"] = "old"
    spy = Spy(world)
    stored = world.docs[fid(CAR)]["profile_fingerprint"]

    await world.run()

    (write,) = spy.writes
    assert set(write["fields"]) == REDERIVE_FIELDS and write["expected"] == stored


async def test_a_directive_success_and_a_failure_write_their_exact_sets(world):
    spy = Spy(world)
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)
    await world.seed(CAR)
    world.profiler.replies[CAR] = status_reply(ProfilerStatus.REFUSED)

    await world.run()

    by_id = {w["id"]: w for w in spy.writes}
    assert set(by_id[fid(DIRECTIVE)]["fields"]) == SUCCESS_FIELDS
    assert set(by_id[fid(CAR)]["fields"]) == FAILURE_FIELDS


async def test_one_embedding_batch_carries_the_joined_labels_and_no_labels_means_no_vector(world):
    await world.seed(CAR, age_days=2)
    await world.seed(POLLEN, age_days=1)
    await world.seed(PASSPORT, age_days=0)
    world.profiler.replies[CAR] = ok(answer(label_words=["vehicle cover", "policy renewal"]))
    world.profiler.replies[POLLEN] = ok(answer(label_words=["hay fever"], subject="health"))
    world.profiler.replies[PASSPORT] = ok(answer(label_words=[], subject="belongings"))

    await world.run()

    assert len(world.embedder.batches) == 1
    assert sorted(world.embedder.batches[0]) == ["hay fever", "vehicle cover, policy renewal"]
    assert world.docs[fid(PASSPORT)]["label_vector"] is None
    assert world.docs[fid(CAR)]["label_vector"] is not None


async def test_places_time_points_and_people_are_derived_and_stored(world):
    await world.seed(TRIP, metadata={"booking": "RM-55120"})
    world.profiler.replies[TRIP] = ok(answer(
        label_words=["hotel stay"], subject="travel", time_role="event",
        places=[{"city": "Rome", "country": "Italy", "source": "Rome"}]))

    await world.run()

    doc = world.docs[fid(TRIP)]
    assert doc["place_keys"] and doc["places_unresolved"] == [] and doc["primary_domain"] == "travel"


async def test_an_event_point_and_a_person_survive_the_checks(world):
    await world.seed(BIRTHDAY)
    world.profiler.replies[BIRTHDAY] = ok(answer(
        label_words=["birthday"], subject="none", time_role="event", about_person="relative",
        time_points=[{"from": "1982-07-14", "to": "", "precision": "day", "source": "14 July 1982"}],
        people=[{"name": "Clara Moreira", "role": "partner", "source": "Clara Moreira"}]))

    await world.run()

    doc = world.docs[fid(BIRTHDAY)]
    assert doc["time_points"][0]["from"] == "1982-07-14" and doc["people"][0]["name"] == "Clara Moreira"
    assert doc["primary_domain"] == "family"


async def test_drops_are_counted_per_reason(world):
    await world.seed(CAR)
    world.profiler.replies[CAR] = ok(answer(label_words=["vehicle cover", "42 things", "insurance"]))

    run = await world.run()

    assert run.drops.get("label_has_number") == 1 and run.drops.get("label_repeats_text") == 1


# ------------------------------------------------------------------------------------------------ races and the baseline

async def test_a_lost_cas_is_counted_and_nothing_else_changes(world):
    await world.seed(CAR)
    await world.seed(TRIP, age_days=-1)
    world.profiler.on_call = lambda text: world.store.interfere(fid(CAR), {"profile_fingerprint": "someone-else"}) \
        if text == CAR else None

    run = await world.run()

    assert (run.lost_cas, run.written) == (1, 1)
    assert world.docs[fid(CAR)]["profile_fingerprint"] == "someone-else" and world.docs[fid(CAR)]["label_words"] is None


async def test_the_baseline_is_refreshed_once_after_any_written_profile(world):
    await world.seed(CAR)
    await world.seed(TRIP)
    await world.seed(PASSPORT)

    run = await world.run()

    assert run.written == 3 and run.refreshed and world.repo.refreshes == [ACC]


async def test_the_baseline_is_refreshed_for_an_ordinary_profile_too_and_for_a_rederivation(world):
    await world.seed(CAR)
    await world.profile_everything()
    world.repo.refreshes.clear()
    world.docs[fid(CAR)]["profile_derivation"] = "old"

    run = await world.run()

    assert (run.rederived, run.refreshed) == (1, True) and world.repo.refreshes == [ACC]


async def test_the_discreet_trade_union_fact_moves_the_baseline_without_changing_identity_or_primary(world):
    await world.seed(UNION)
    world.profiler.replies[UNION] = ok(answer(sensitivity="discreet", subject="money", label_words=["union dues"]))

    run = await world.run()

    assert run.refreshed and world.docs[fid(UNION)]["sensitivity"] == "discreet"
    assert world.repo.refreshes == [ACC]


async def test_no_refresh_when_only_failures_lost_writes_or_nothing(world):
    await world.seed(CAR)
    world.profiler.replies[CAR] = status_reply(ProfilerStatus.INVALID)

    failed = await world.run()
    nothing = await World().run()

    assert failed.refreshed is False and nothing.refreshed is False and world.repo.refreshes == []


async def test_a_failing_refresh_does_not_fail_the_run(world, caplog):
    await world.seed(CAR)

    async def boom(owner_id, *a, **k):
        raise RuntimeError("cache down")
    world.repo.refresh_biographical_context_cache = boom

    with caplog.at_level(logging.WARNING):
        run = await world.run()

    assert run.written == 1 and run.refreshed is False and run.error is False


async def test_writing_stops_when_the_lease_has_no_time_left(world):
    for n in range(3):
        await world.seed(f"Alex keeps record number {n}", age_days=n)
    world.profiler.on_call = lambda text: world.clock.advance(5000)
    until = world.clock.now + LEASE_MARGIN + timedelta(seconds=BUDGET + 1000)

    run = await world.service.run(ACC, until)

    assert run.called == 3 and run.written == 0 and run.deferred == 3


# ------------------------------------------------------------------------------------------------ errors and logs

async def test_a_scan_error_is_returned_not_raised(world, monkeypatch, caplog):
    async def boom(account_id):
        raise RuntimeError("store down")
    monkeypatch.setattr(world.repo, "list_profile_states", boom)

    with caplog.at_level(logging.ERROR):
        run = await world.run()

    assert run.error is True and run.written == 0
    records = [r for r in caplog.records if "run failed account=" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is not None


async def test_an_embedding_error_is_an_error_run_with_nothing_written(world):
    await world.seed(CAR)

    async def boom(texts, task_type="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("embeddings down")
    world.embedder.get_embeddings_batch = boom

    run = await world.run()

    assert run.error and world.docs[fid(CAR)]["profile_fingerprint"] is None


async def test_a_raising_profiler_is_an_outage_not_a_crash(world):
    await world.seed(CAR)

    async def boom(account_id, text, metadata):
        raise RuntimeError("provider exploded")
    world.profiler.profile = boom

    run = await world.run()

    assert run.outage and not run.error and run.failed == 0


async def test_the_logs_carry_ids_and_counts_but_never_fact_text(world, caplog):
    await world.seed(CAR)
    await world.seed(DIRECTIVE, domain=FactDomain.AGENT_DIRECTIVE)
    await world.seed(TRIP)
    world.profiler.replies[TRIP] = status_reply(ProfilerStatus.INVALID)

    with caplog.at_level(logging.DEBUG):
        run = await world.run()

    lines = [r.getMessage() for r in caplog.records if "[FactProfile]" in r.getMessage()]
    assert any(l.startswith("[FactProfile] run account=acc-1 scanned=3") and "usd=" in l for l in lines)
    fact_lines = [l for l in lines if " fact=" in l]
    assert len(fact_lines) == 3 and all("outcome=" in l and "ms=" in l and "usd=" in l for l in fact_lines)
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for word in ("Example Mutual", "Albergo", "RM-55120", "English", "vehicle cover"):
        assert word not in everything
    assert run.written == 2

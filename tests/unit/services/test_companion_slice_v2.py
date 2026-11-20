"""The companion's read of the owner's memory on the fact profile (RFC §6 rows 6 and 7).

- Standing directives: one repository read whose condition is the marker `is_directive` reads (`DIRECTIVE_DOMAIN`),
  on the evaluating fact store: what comes back is what the predicate accepts.
- The biographical slice goes through `FactSearchPort.search`: `subject` and `message` from the first phrase,
  `domains` from the session's domains as registry keys (an old stored value mapped through the registry, an unknown
  one dropped with a warning), the first ten facts. The search here is a fake with semantics: it keeps an account's
  facts whose profile `domain_keys` meet the query's domains (all of them when none), newest first.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import AbstractSet, List, Sequence

import pytest

from src.composition.companion_setup import build_companions
from src.config.environment import EnvironmentConfig
from src.domain.entities import FactEntity, FactType
from src.domain.fact_domains import Domain
from src.domain.fact_lifecycle import is_directive
from src.domain.fact_query import FactQuery
from src.domain.fact_search_result import PhraseSearchResult
from src.ports.fact_search_port import FactSearchPort
from src.services.companion_context_assembler_service import CompanionContextAssemblerService
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_lifecycle_world import make_fact_world
from tests.support.firestore_eval import EvalDb

USER = "usr-1"


class FakeFactSearch(FactSearchPort):
    """An account's facts filtered by the query's domains against each fact's `domain_keys`, newest first; every query
    it answered is kept for the test to read."""

    def __init__(self, facts: Sequence[FactEntity]) -> None:
        self._facts = list(facts)
        self.queries: List[FactQuery] = []

    async def search(self, account_id: str, query: FactQuery, *, exclude: AbstractSet[str] = frozenset()
                     ) -> List[FactEntity]:
        self.queries.append(query)
        mine = [f for f in self._facts if f.account_id == account_id and f.id not in exclude]
        if query.domains:
            mine = [f for f in mine if set(f.domain_keys or ()) & set(query.domains)]
        return sorted(mine, key=lambda f: f.created_at, reverse=True)

    async def search_phrases(self, account_id: str, queries: Sequence[FactQuery]) -> PhraseSearchResult:
        raise AssertionError("the companion slice searches one phrase")


class _NoSummary:
    async def get_summary(self, session_id: str):
        return None


def _profiled(text: str, primary: str, keys: Sequence[str], **over) -> FactEntity:
    return make_fact(text, primary_domain=primary, domain_keys=list(keys), **over)


YOGA = _profiled("morning yoga 30 min", Domain.SPORT, [Domain.SPORT])
TENNIS = _profiled("plays tennis on Sundays", Domain.SPORT, [Domain.SPORT], age_days=1)
INSURANCE = _profiled("Alex's car insurance with Example Mutual Insurance", Domain.TRANSPORT,
                      [Domain.TRANSPORT, Domain.MONEY], age_days=2)
BIRTH = _profiled("Alex Doe was born 1980-01-01 in Springfield", Domain.BIOGRAPHY, [Domain.BIOGRAPHY], age_days=3)
FOREIGN = _profiled("plays tennis on Sundays (another account)", Domain.SPORT, [Domain.SPORT], account=OTHER)


def _assembler(search: FactSearchPort, fact_repo=None, limit: int = 10) -> CompanionContextAssemblerService:
    return CompanionContextAssemblerService(
        companion_repo=None, cache_repo=_NoSummary(), embedding_service=None,  # type: ignore[arg-type]
        fact_search=search, fact_repo=fact_repo, biographical_limit=limit,  # type: ignore[arg-type]
    )


async def _slice(search: FactSearchPort, phrases: List[str], domains=None, limit: int = 10) -> List[FactEntity]:
    ctx = await _assembler(search, limit=limit).assemble_context(
        session_id="slack:C1", account_id=ACC, query_phrases=phrases, include_own_records=False,
        include_biographical=True, session_domains=domains, user_id=USER,
    )
    return ctx.biographical_facts


# ---------------------------------------------------------------------------------------- standing directives

async def test_directives_are_the_facts_the_predicate_accepts():
    _, repo, _ = make_fact_world()
    stored = [
        make_fact("suggest only vegetarian recipes", directive=True),
        make_fact("answer in English", directive=True, age_days=1),
        make_fact("plays tennis on Sundays", type=FactType.PRINCIPLE),
        make_fact("morning yoga 30 min", tags=["mindset"]),
        make_fact("suggest only vegetarian recipes (another account)", directive=True, account=OTHER),
    ]
    for fact in stored:
        await repo.add_fact(fact)

    ctx = await _assembler(FakeFactSearch([]), fact_repo=repo).assemble_context(
        session_id="slack:C1", account_id=ACC, query_phrases=[], include_own_records=False,
        include_standing_directives=True,
    )

    assert {f.text for f in ctx.standing_directives} == {
        f.text for f in stored if is_directive(f) and f.account_id == ACC} == {"suggest only vegetarian recipes", "answer in English"}


# ------------------------------------------------------------------------------------------ biographical slice

async def test_the_slice_searches_the_first_phrase_as_subject_and_message():
    search = FakeFactSearch([YOGA, TENNIS, INSURANCE, BIRTH, FOREIGN])

    got = await _slice(search, ["how is the training going", "a second phrase"])

    assert [f.text for f in got] == [BIRTH.text, INSURANCE.text, TENNIS.text, YOGA.text]
    [query] = search.queries
    assert (query.subject, query.message, query.domains, query.path) == (
        "how is the training going", "how is the training going", (), "companion")
    assert query.today == datetime.now(timezone.utc).date()


async def test_registry_session_domains_narrow_the_slice():
    search = FakeFactSearch([YOGA, TENNIS, INSURANCE, BIRTH])

    got = await _slice(search, ["sport this week"], domains=[Domain.SPORT])

    assert [f.text for f in got] == [TENNIS.text, YOGA.text]
    assert search.queries[0].domains == (Domain.SPORT,)


async def test_an_old_stored_session_domain_is_mapped_through_the_registry():
    search = FakeFactSearch([YOGA, TENNIS, INSURANCE, BIRTH])

    got = await _slice(search, ["the car"], domains=["finance"])

    assert [f.text for f in got] == [INSURANCE.text]
    assert search.queries[0].domains == (Domain.MONEY, Domain.SAVINGS)


async def test_an_old_enum_member_is_mapped_like_its_stored_value():
    search = FakeFactSearch([YOGA, BIRTH])

    got = await _slice(search, ["who am I"], domains=["biographical"])

    assert [f.text for f in got] == [BIRTH.text]
    assert search.queries[0].domains == (Domain.BIOGRAPHY,)


async def test_an_unknown_session_domain_is_dropped_with_a_warning(caplog):
    search = FakeFactSearch([YOGA, TENNIS, INSURANCE])

    with caplog.at_level(logging.WARNING):
        got = await _slice(search, ["sport"], domains=["astrology", Domain.SPORT])

    assert [f.text for f in got] == [TENNIS.text, YOGA.text]
    assert search.queries[0].domains == (Domain.SPORT,)
    assert any("astrology" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_only_unknown_session_domains_search_without_a_domain():
    search = FakeFactSearch([YOGA, INSURANCE])

    got = await _slice(search, ["anything"], domains=["astrology"])

    assert {f.text for f in got} == {YOGA.text, INSURANCE.text}
    assert search.queries[0].domains == ()


async def test_the_slice_keeps_the_first_ten_facts():
    facts = [_profiled(f"morning yoga 30 min, day {n}", Domain.SPORT, [Domain.SPORT], age_days=n) for n in range(12)]

    got = await _slice(FakeFactSearch(facts), ["yoga"])

    assert [f.text for f in got] == [f"morning yoga 30 min, day {n}" for n in range(11, 1, -1)]


async def test_the_slice_reads_only_the_turns_account():
    got = await _slice(FakeFactSearch([FOREIGN]), ["tennis"])

    assert got == []


async def test_no_phrases_search_an_empty_subject():
    search = FakeFactSearch([YOGA])

    got = await _slice(search, [], domains=[Domain.SPORT])

    assert [f.text for f in got] == [YOGA.text]
    assert (search.queries[0].subject, search.queries[0].message) == ("", "")


# ---------------------------------------------------------------------------------------------------- wiring

@pytest.fixture
def companions(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    search = FakeFactSearch([])
    container = SimpleNamespace(repository=object(), embedding_service=object(), context_builder=object(),
                                assembly_service=object(), prompt_content_store=object(), user_drive_service=object())
    languages = SimpleNamespace(resolver=object(), localization_port=object())
    built = build_companions(EvalDb(), EnvironmentConfig(), container=container, user_repo=object(),  # type: ignore[arg-type]
                             quota_service=object(), extraction_queue=object(), notification_service=object(),
                             languages=languages, fact_search=search)
    return built, search


async def test_the_built_assembler_searches_the_given_fact_search_and_keeps_ten(companions):
    built, search = companions
    search._facts = [_profiled(f"plays tennis on Sundays, week {n}", Domain.SPORT, [Domain.SPORT], age_days=n)
                     for n in range(11)]

    ctx = await built.context_assembler.assemble_context(
        session_id="slack:C1", account_id=ACC, query_phrases=["tennis"], include_own_records=False,
        include_biographical=True, user_id=USER,
    )

    assert len(search.queries) == 1 and len(ctx.biographical_facts) == 10


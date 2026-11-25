"""The triage pass's tools (MCP_MEMORY_SUBMISSIONS_RFC §7.4): every refused candidate names its argument and holds
nothing; a cited fact is followed to its current version and its stored text (Review Focus 5); a submission may feed
two candidates but is never both cited and discarded; `count_words` reports against sixty. The facts live on the
evaluating fake behind the real repository."""
from typing import Any, Dict

import pytest

from src.domain.memory_inbox import Basis, DiscardReason, RelationKind
from src.infrastructure.memory_inbox_triage_scope import MemoryInboxTriageScope
from tests.support.fact_kit import ACC, OTHER, put_raw
from tests.support.fact_store import FactStore

CURRENT_ID, OLD_ID, OTHER_ACCOUNT_FACT = "f-dentist-2", "f-dentist-1", "f-other"
CURRENT_TEXT = "User's dentist is Dr. Marta Quillen at Clinica Ribeira"

OK: Dict[str, Any] = dict(
    submission_ids=["s1"], text="Alex plays tennis on Sundays", happens_when="every Sunday",
    said_when="", basis="said", directive=False, temporal_class="dynamic", context_priority="medium",
    expiry={"kind": "horizon", "days": 90}, relation="new", fact_id="",
)


@pytest.fixture
def facts():
    store = FactStore()
    put_raw(store, OLD_ID, "User's dentist is Dr. Marta Quillen", entity={"lineage_id": "lin-dentist"},
            state="superseded", superseded_by=CURRENT_ID, is_current=False)
    put_raw(store, CURRENT_ID, CURRENT_TEXT, entity={"lineage_id": "lin-dentist", "version": 2})
    put_raw(store, OTHER_ACCOUNT_FACT, "Another owner's dentist", entity={"account_id": OTHER})
    return store


@pytest.fixture
def scope(facts):
    return MemoryInboxTriageScope(["s1", "s2", "s3"], ACC, facts.repo)


@pytest.mark.parametrize("args, argument", [
    (dict(OK, submission_ids=["s9"]), "submission_ids"), (dict(OK, submission_ids=[]), "submission_ids"),
    (dict(OK, text=""), "text"), (dict(OK, text="one\ntwo"), "text"), (dict(OK, text=" ".join(["w"] * 61)), "text"),
    (dict(OK, said_when=5), "said_when"), (dict(OK, happens_when="a\nb"), "happens_when"),
    (dict(OK, basis="heard"), "basis"), (dict(OK, directive="no"), "directive"),
    (dict(OK, temporal_class="FOREVER"), "temporal_class"), (dict(OK, context_priority="urgent"), "context_priority"),
    (dict(OK, expiry={"kind": "soon"}), "expiry"), (dict(OK, relation="similar"), "relation"),
    (dict(OK, relation="refines", fact_id=""), "fact_id"),
    (dict(OK, relation="contradicts", fact_id=OTHER_ACCOUNT_FACT), "fact_id"),
    (dict(OK, relation="duplicates", fact_id="f-nowhere"), "fact_id")])
async def test_a_refused_candidate_names_its_argument_and_records_nothing(scope, args, argument):
    result = await scope.record_candidate(args)
    assert set(result) == {"error"} and result["error"].startswith(f"{argument}: ")
    assert scope.decisions().candidates == ()


async def test_an_over_long_text_is_refused_with_its_count(scope):
    result = await scope.record_candidate(dict(OK, text=" ".join(["w"] * 61)))
    assert result["error"].startswith("text: 61 words, at most 60")


async def test_a_candidate_is_held_with_its_draft_in_canonical_form(scope):
    result = await scope.record_candidate(dict(OK, basis="Document", temporal_class="DYNAMIC",
                                               context_priority="historical", expiry={"kind": "date", "date": "2027-03-08"},
                                               text="  Alex trains with Robin at the Iron Temple gym  ", said_when=None))
    held = scope.decisions().candidates[0]
    assert result == {"status": "recorded", "submission_ids": ["s1"]}
    assert (held.fields.text, held.fields.said_when, held.fields.basis) == \
           ("Alex trains with Robin at the Iron Temple gym", "", Basis.DOCUMENT)
    assert (held.draft.temporal_class, held.draft.context_priority, dict(held.draft.expiry)) == \
           ("dynamic", "archival", {"kind": "date", "date": "2027-03-08"})
    assert (held.relation.kind, held.relation.fact_id, held.relation.fact_text) == (RelationKind.NEW, None, None)


async def test_a_past_expiry_date_is_not_refused(scope):
    await scope.record_candidate(dict(OK, expiry={"kind": "date", "date": "2020-01-01"}))
    assert dict(scope.decisions().candidates[0].draft.expiry) == {"kind": "date", "date": "2020-01-01"}


async def test_a_superseded_fact_id_resolves_to_the_current_version_and_its_stored_text(scope):
    await scope.record_candidate(dict(OK, relation="refines", fact_id=OLD_ID))
    d = scope.decisions().candidates[0].relation
    assert (d.kind, d.fact_id, d.fact_text) == (RelationKind.REFINES, CURRENT_ID, CURRENT_TEXT)


async def test_one_submission_may_feed_two_candidates(scope):
    await scope.record_candidate(dict(OK, submission_ids=["s1"], text="Alex plays tennis on Sundays"))
    await scope.record_candidate(dict(OK, submission_ids=["s1", "s2"], text="The Iron Temple gym opened a pool"))
    assert [c.source_ids for c in scope.decisions().candidates] == [("s1",), ("s1", "s2")]


async def test_cited_then_discarded_and_discarded_then_cited_are_refused(scope):
    await scope.record_candidate(dict(OK, submission_ids=["s1"]))
    refused = await scope.discard_submissions({"submission_ids": ["s1", "s2"], "reason": "known"})
    assert refused["error"].startswith("submission_ids") and "s1" in refused["error"]
    assert await scope.discard_submissions({"submission_ids": ["s2"], "reason": "trivial"}) == \
           {"status": "discarded", "submission_ids": ["s2"], "reason": "trivial"}
    again = await scope.record_candidate(dict(OK, submission_ids=["s2"]))
    assert again["error"].startswith("submission_ids") and "s2" in again["error"]
    decisions = scope.decisions()
    assert [c.source_ids for c in decisions.candidates] == [("s1",)]
    assert dict(decisions.discards) == {"s2": DiscardReason.TRIVIAL}


@pytest.mark.parametrize("args", [{"submission_ids": ["s9"], "reason": "known"},
                                  {"submission_ids": ["s1"], "reason": "undecided"},
                                  {"submission_ids": ["s1"], "reason": None}])
async def test_a_refused_discard_holds_nothing(scope, args):
    assert "error" in await scope.discard_submissions(args)
    assert dict(scope.decisions().discards) == {}


async def test_a_second_discard_replaces_the_reason(scope):
    await scope.discard_submissions({"submission_ids": "s3", "reason": "Known"})
    await scope.discard_submissions({"submission_ids": ["s3"], "reason": "waiting"})
    assert dict(scope.decisions().discards) == {"s3": DiscardReason.WAITING}


def test_count_words_reports_against_sixty(scope):
    assert scope.count_words({"text": " ".join(["w"] * 61)}) == \
           {"word_count": 61, "limit": 60, "within_limit": False, "excess": 1}

"""Which action a review write is (Facts S4 · T1b review H2): the closing state, the version rule, the revive."""
import pytest

from src.domain.entities import FactState, TemporalClass
from src.domain.fact_expiry import EXPIRY_NONE
from src.domain.fact_review_rules import STAYS_STALE_REFUSAL, basis_refusal, review_action
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_plan import content_changed, plan_fact_update
from tests.support.review_rule_builders import NOW, fact

STALE, CURRENT = FactState.STALE, FactState.CURRENT
U = ParsedFactUpdate


@pytest.mark.parametrize("current_state", [STALE, CURRENT])
@pytest.mark.parametrize(
    "update,expected",
    [
        (U(state=FactState.SUPERSEDED, superseded_by="x"), "supersede"),
        (U(state=FactState.ARCHIVED), "archive"),
        (U(state=FactState.INVALIDATED), "invalidate"),
        (U(content="Takes vitamin D3", state=FactState.ARCHIVED), "archive"),
        (U(tags=["health"], state=FactState.INVALIDATED), "invalidate"),
        (U(content="Takes vitamin D3"), "rewrite"),
        (U(content="Takes vitamin D3", state=CURRENT), "rewrite"),
        (U(state=CURRENT), "revive"),
    ],
)
def test_each_state_and_content_cell(current_state, update, expected):
    assert review_action(fact(state=current_state), update) == expected


@pytest.mark.parametrize("update", [U(tags=["health"]), U(metadata={"dose": "2000 IU"}), U(temporal_class=TemporalClass.DYNAMIC)])
def test_a_change_of_tags_metadata_or_class_is_a_rewrite_not_a_revive(update):
    stale = fact(tags=["old"], metadata={}, temporal_class=TemporalClass.STABLE)
    assert review_action(stale, update) == "rewrite"
    assert review_action(stale, U(**{**update.__dict__, "state": CURRENT})) == "rewrite"
    assert plan_fact_update(stale, update, NOW).kind == "version"


def test_a_tag_order_difference_alone_is_no_change():
    stale = fact(tags=["a", "b"])
    assert review_action(stale, U(tags=["b", "a"], state=CURRENT)) == "revive"
    assert not content_changed(stale, U(tags=["b", "a"]))


def test_the_same_text_is_no_change():
    assert review_action(fact(), U(content="Takes vitamin D", state=CURRENT)) == "revive"


def test_a_stale_fact_with_no_state_and_no_content_change_stays_stale():
    assert review_action(fact(state=STALE), U()) is None
    assert review_action(fact(state=STALE), U(expiry=EXPIRY_NONE)) is None
    assert review_action(fact(state=STALE), U(reported_date=NOW)) is None


def test_a_current_fact_given_a_new_expiry_is_renewed():
    assert review_action(fact(state=CURRENT), U(expiry=EXPIRY_NONE)) == "revive"
    assert review_action(fact(state=CURRENT), U()) is None


def test_the_action_agrees_with_the_version_rule_of_plan_fact_update():
    cases = [U(content="x"), U(tags=["t"]), U(state=CURRENT), U(state=FactState.ARCHIVED), U(content="y", state=FactState.ARCHIVED)]
    for update in cases:
        action = review_action(fact(), update)
        is_version = plan_fact_update(fact(), update, NOW).kind == "version"
        assert is_version == content_changed(fact(), update)
        if action == "rewrite":
            assert is_version
        if action == "revive":
            assert not is_version


def test_a_call_that_changes_nothing_is_refused_with_the_stay_stale_text():
    refusal = basis_refusal(
        fact(), U(), None, named_fact=None, question=None, made_current=frozenset(), history=None,
        today=NOW.date(), tz="UTC",
    )
    assert refusal == STAYS_STALE_REFUSAL and "reported_date is kept by the system" in refusal


def test_the_basis_rules_follow_the_action_the_update_makes_not_the_callers_word():
    # content changed with state CURRENT: a rewrite, so a revive's wording does not apply
    tags_only = basis_refusal(
        fact(), U(tags=["t"], state=CURRENT), None, named_fact=None, question=None, made_current=frozenset(),
        history=None, today=NOW.date(), tz="UTC",
    )
    assert tags_only is not None and tags_only.startswith("basis: rewrite needs a basis")
    closed_with_content = basis_refusal(
        fact(), U(content="x", state=FactState.ARCHIVED), None, named_fact=None, question=None, made_current=frozenset(),
        history=None, today=NOW.date(), tz="UTC",
    )
    assert closed_with_content is not None and closed_with_content.startswith("basis: archive needs a basis")

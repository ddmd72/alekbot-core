"""S3-D12 (T2b review H1): on a cleanup pass the rewrite a born-due refusal asks for has other text, so it must count
as the answer: a merge of the same facts, a create from the same facts, or a create that keeps the refused words."""
import pytest

from src.domain.consolidation_pass_stats import OUTCOME_BORN_DUE, OUTCOME_ERROR, OUTCOME_OK, ConsolidationPassStats

USER = "abcdef1234567890-secret-tail"
CLEANUP = ["stage2a", "stage2b", "cluster"]


def stats(label):
    return ConsolidationPassStats(label, "model-x", "medium", 15, USER)


def turn(s, call, outcome):
    s.record_turn([call])
    s.record_outcomes([call], [outcome])


def create(text, *, derived=None, domain="biographical"):
    attrs = {"domain": domain}
    if derived is not None:
        attrs["derived_from"] = derived
    return ("create_fact", {"content": text, "fact_attributes": attrs})


def merge(text, ids, domain="biographical"):
    return ("merge_facts", {"merged_content": text, "fact_ids": ids, "fact_attributes": {"domain": domain}})


@pytest.mark.parametrize("label", CLEANUP)
def test_a_past_tense_rewrite_of_a_refused_create_answers_it(label):
    s = stats(label)
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("Took a trip to Lisbon on 2026-10-10"), OUTCOME_OK)

    assert (s.born_due, s.abandoned) == (1, 0)


@pytest.mark.parametrize("label", CLEANUP)
def test_a_past_tense_rewrite_of_a_refused_merge_answers_it(label):
    s = stats(label)
    turn(s, merge("Will visit Lisbon on 2026-10-10 with Ana", ["a", "b"]), OUTCOME_BORN_DUE)
    turn(s, merge("Visited Lisbon with Ana", ["b", "a"]), OUTCOME_OK)

    assert s.abandoned == 0


@pytest.mark.parametrize("label", CLEANUP)
def test_a_heavy_rewrite_that_keeps_few_words_alerts_even_with_the_same_derived_from(label):
    s = stats(label)
    turn(s, create("Meets the dentist on 2026-10-10", derived=["f1"]), OUTCOME_BORN_DUE)
    turn(s, create("Went to a dental check-up", derived=["f1"]), OUTCOME_OK)

    assert s.abandoned == 1                                  # the accepted direction: errs towards an alert


@pytest.mark.parametrize("label", CLEANUP)
def test_a_dropped_part_next_to_an_unrelated_create_is_still_abandoned(label):
    s = stats(label)
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("Prefers window seats on long flights"), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_an_overlapping_create_of_another_domain_does_not_answer(label):
    s = stats(label)
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("Took a trip to Lisbon on 2026-10-10", domain="health"), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_a_merge_of_other_facts_does_not_answer_a_refused_merge(label):
    s = stats(label)
    turn(s, merge("Will visit Lisbon on 2026-10-10", ["a", "b"]), OUTCOME_BORN_DUE)
    turn(s, merge("Visited Lisbon", ["a", "c"]), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_a_create_from_other_facts_does_not_answer_a_split_part(label):
    s = stats(label)
    turn(s, create("Meets the dentist on 2026-10-10", derived=["f1"]), OUTCOME_BORN_DUE)
    turn(s, create("Took a trip to Lisbon", derived=["f2"]), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_a_dropped_part_beside_a_sibling_with_the_same_derived_from_is_abandoned(label):
    s = stats(label)
    turn(s, create("The owner flew to Lisbon on 2026-10-10", derived=["f1"]), OUTCOME_BORN_DUE)
    turn(s, create("The owner works as a data engineer at Acme", derived=["f1"]), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_the_lisbon_rewrite_answers_with_and_without_derived_from(label):
    for derived in (None, ["f1"]):
        s = stats(label)
        turn(s, create("Plans a trip to Lisbon on 2026-10-10", derived=derived), OUTCOME_BORN_DUE)
        turn(s, create("Took a trip to Lisbon on 2026-10-10", derived=derived), OUTCOME_OK)

        assert s.abandoned == 0


@pytest.mark.parametrize("refused,other", [
    ("User plans to fly to Lisbon on 2026-10-10", "User plans to renew the passport by 2026-12-01"),
    ("User has a dentist appointment on 2026-10-20", "User has a car service appointment on 2026-11-05"),
])
@pytest.mark.parametrize("label", CLEANUP)
def test_shared_boilerplate_and_dates_do_not_make_an_unrelated_create_the_answer(label, refused, other):
    s = stats(label)
    turn(s, create(refused), OUTCOME_BORN_DUE)
    turn(s, create(other), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
def test_a_failed_or_closing_follow_up_answers_nothing(label):
    s = stats(label)
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("Took a trip to Lisbon on 2026-10-10"), OUTCOME_ERROR)

    assert s.abandoned == 1


def test_the_rewrite_rule_does_not_reach_a_missing_expiry_refusal():
    s = stats("stage2a")
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), "expiry")
    turn(s, create("Took a trip to Lisbon on 2026-10-10"), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", ["stage1", "email"])
def test_a_conversation_pass_is_unchanged(label):
    s = stats(label)
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("Prefers window seats"), OUTCOME_OK)

    assert s.abandoned == 0


def test_the_alert_never_carries_the_text():
    s = stats("stage2a")
    turn(s, create("Plans a trip to Lisbon on 2026-10-10"), OUTCOME_BORN_DUE)

    assert "Lisbon" not in s.format_abandoned_alert()

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


# Ten realistic past-tense rewrites in the house style, each the answer the refusal asks for (T2b re-review 2, M2).
ANSWERED = [
    ("User plans to move to Valencia in September 2026", "User moved to Valencia in September 2026"),
    ("User intends to submit the tax return by 2026-06-30", "User submitted the tax return in June 2026"),
    ("User will attend the Madrid conference on Oct 15", "User attended the Madrid conference on Oct 15 and gave a talk"),
    ("User is scheduled for knee surgery on 2026-10-05", "User had knee surgery on 2026-10-05; recovery went well"),
    ("User has a dentist appointment on 2026-10-12", "User had the dentist appointment on 2026-10-12; no cavities"),
    ("User expects the visa decision by 2026-10-30", "User received the visa decision on 2026-10-28"),
    ("User starts the new job at Acme on 2026-10-01", "User started the new job at Acme"),
    ("User is going to the Rome marathon on 2026-10-18", "User ran the Rome marathon in 3h 45m"),
]
NOT_ANSWERED = [        # the two noted losses: fly/flew leaves exactly half, and the car sale is a real heavy rewrite
    ("User plans to fly to Lisbon on 2026-10-10 for the PyCon conference", "User flew to Lisbon on 2026-10-10 for PyCon"),
    ("Plans to sell the car before 2026-10-01", "Sold the car in September 2026 for 9000 EUR"),
]


@pytest.mark.parametrize("label", CLEANUP)
@pytest.mark.parametrize("refused,rewrite", ANSWERED)
def test_a_realistic_past_tense_rewrite_answers_the_refusal(label, refused, rewrite):
    s = stats(label)
    turn(s, create(refused), OUTCOME_BORN_DUE)
    turn(s, create(rewrite), OUTCOME_OK)

    assert s.abandoned == 0


@pytest.mark.parametrize("refused,rewrite", NOT_ANSWERED)
def test_a_rewrite_that_keeps_half_or_fewer_of_the_words_alerts(refused, rewrite):
    s = stats("stage2a")
    turn(s, create(refused), OUTCOME_BORN_DUE)
    turn(s, create(rewrite), OUTCOME_OK)

    assert s.abandoned == 1


@pytest.mark.parametrize("label", CLEANUP)
@pytest.mark.parametrize("refused,other", [
    ("User plans to fly to Lisbon on 2026-10-10", "User plans to renew the passport by 2026-12-01"),
    ("User will attend the Madrid conference on Oct 15", "User attended a wedding in Kyiv"),
])
def test_unrelated_creates_still_alert_under_containment(label, refused, other):
    s = stats(label)
    turn(s, create(refused), OUTCOME_BORN_DUE)
    turn(s, create(other), OUTCOME_OK)

    assert s.abandoned == 1


def test_exactly_half_of_the_words_is_not_enough():
    s = stats("stage2a")                # dentist, appointment vs car, service, appointment: 1 of 2
    turn(s, create("User has a dentist appointment on 2026-10-20"), OUTCOME_BORN_DUE)
    turn(s, create("User has a car service appointment on 2026-11-05"), OUTCOME_OK)

    assert s.abandoned == 1


def test_the_residual_a_near_duplicate_of_the_same_subject_is_taken_as_the_answer():
    s = stats("stage2a")
    turn(s, create("User plans a trip to Lisbon with Ana on 2026-10-10"), OUTCOME_BORN_DUE)
    turn(s, create("User plans a trip to Porto with Ana on 2026-11-10"), OUTCOME_OK)

    assert s.abandoned == 0


def test_a_split_sibling_with_the_same_derived_from_still_alerts_under_containment():
    s = stats("stage2a")
    turn(s, create("User flew to Lisbon on 2026-10-10", derived=["f1"]), OUTCOME_BORN_DUE)
    turn(s, create("User works as a data engineer at Acme", derived=["f1"]), OUTCOME_OK)

    assert s.abandoned == 1

"""The born-due refusal texts, one per `RefusalWording` (Facts S4 plan §3.6, T2): Stage 1 and cleanup unchanged byte for
byte, the review's text for the review and the owner, and never the DISCARD text for them."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactBornDueError
from src.domain.fact_expiry import NOTHING_WRITTEN, Expiry
from src.domain.fact_lifecycle import born_due, compute_expiry
from src.domain.fact_write_context import REVIEW_REPORTED_DATE_NOTE
from src.domain.pass_actor import PassActor
from src.domain.refusal_wording import RefusalWording

UTC = timezone.utc
LISBON = "Europe/Lisbon"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)  # 10:00 in Europe/Lisbon
REPORTED = NOW - timedelta(days=90)  # 2026-07-26 in Europe/Lisbon
PAST_DATE = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 20)), REPORTED, LISBON)
TODAY_DATE = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2026, 10, 24)), REPORTED, LISBON)
DUE_HORIZON = compute_expiry(Expiry(ExpiryKind.HORIZON, days=14), REPORTED, LISBON)  # ended 2026-08-09

REVIEW_WAY_OUT = (
    "this pass reviews stored memory: give a horizon (counted from now), or archive with basis date_passed when its date "
    "is over; doing nothing keeps the fact hidden"
)

EXPECTED_PAST_DATE = {
    RefusalWording.STAGE1: (
        "the date 2026-10-20 is past (today is 2026-10-24 in Europe/Lisbon): DISCARD the candidate if it no longer "
        "matters, or rewrite the fact as what happened (past tense, the outcome if known) with {kind: none}. "
        f"{NOTHING_WRITTEN}"
    ),
    RefusalWording.CLEANUP: (
        "the date 2026-10-20 is past (today is 2026-10-24 in Europe/Lisbon): this pass reviews stored memory: do not "
        "drop the fact because its date passed. Rewrite it as what happened (past tense, the outcome if known) with "
        f"{{kind: none}}, or give a future date if the plan still holds. {NOTHING_WRITTEN}"
    ),
    RefusalWording.REVIEW: (
        f"the date 2026-10-20 is past (today is 2026-10-24 in Europe/Lisbon): {REVIEW_WAY_OUT}. {NOTHING_WRITTEN}"
    ),
}
EXPECTED_DUE_HORIZON = {
    RefusalWording.STAGE1: (
        "counted from when the owner said it (2026-07-26), a horizon of 14 days ended 2026-08-09: give at least 93 "
        f"days, rewrite the fact as history with {{kind: none}}, or DISCARD it. {NOTHING_WRITTEN}"
    ),
    RefusalWording.CLEANUP: (
        "counted from when the owner said it (2026-07-26), a horizon of 14 days ended 2026-08-09: this pass reviews "
        "stored memory: do not drop the fact. Give at least 93 days, or rewrite it as what happened (past tense, the "
        f"outcome if known) with {{kind: none}}. {NOTHING_WRITTEN}"
    ),
    RefusalWording.REVIEW: f"this horizon ended 2026-08-09, too soon: {REVIEW_WAY_OUT}. {NOTHING_WRITTEN}",
}
EXPECTED_TODAY_DATE = {
    RefusalWording.STAGE1: (
        "a plan for today is not over yet: give a short horizon of at least 93 days so it is reviewed after it "
        f"happened. {NOTHING_WRITTEN}"
    ),
    RefusalWording.CLEANUP: (
        "a plan for today is not over yet: give a short horizon of at least 93 days so it is reviewed after it "
        f"happened. {NOTHING_WRITTEN}"
    ),
    RefusalWording.REVIEW: f"a plan for today is not over yet: {REVIEW_WAY_OUT}. {NOTHING_WRITTEN}",
}


@dataclass(frozen=True)
class Rule:
    """A row that refuses every due horizon, so each wording's horizon text can be read (no real cleanup row does)."""

    refusal_wording: RefusalWording
    floors_due_horizon: bool = False


def refusal(fields, rule) -> str:
    with pytest.raises(FactBornDueError) as refused:
        born_due(fields, REPORTED, NOW, LISBON, rule)
    return str(refused.value)


def test_every_wording_has_its_texts():
    assert set(EXPECTED_PAST_DATE) == set(EXPECTED_DUE_HORIZON) == set(EXPECTED_TODAY_DATE) == set(RefusalWording)


@pytest.mark.parametrize("wording", list(RefusalWording))
def test_the_past_date_text_of_each_wording(wording):
    assert refusal(PAST_DATE, Rule(wording)) == EXPECTED_PAST_DATE[wording]


@pytest.mark.parametrize("wording", list(RefusalWording))
def test_the_due_horizon_text_of_each_wording(wording):
    assert refusal(DUE_HORIZON, Rule(wording)) == EXPECTED_DUE_HORIZON[wording]


@pytest.mark.parametrize("wording", list(RefusalWording))
def test_the_date_today_text_of_each_wording(wording):
    assert refusal(TODAY_DATE, Rule(wording)) == EXPECTED_TODAY_DATE[wording]


@pytest.mark.parametrize("wording", list(RefusalWording))
def test_a_flooring_row_floors_a_due_horizon_whatever_its_wording(wording):
    got, note = born_due(DUE_HORIZON, REPORTED, NOW, LISBON, Rule(wording, floors_due_horizon=True))
    assert got["expires_at"] == NOW + timedelta(days=2)
    assert note == "counted from 2026-07-26 this horizon had already passed; the fact goes to review on 2026-10-26"


@pytest.mark.parametrize("actor", list(PassActor))
@pytest.mark.parametrize("fields", [PAST_DATE, TODAY_DATE])
def test_each_pass_gets_its_rows_date_text(actor, fields):
    expected = (EXPECTED_PAST_DATE if fields is PAST_DATE else EXPECTED_TODAY_DATE)[actor.refusal_wording]
    assert refusal(fields, actor) == expected


@pytest.mark.parametrize("actor", [a for a in PassActor if not a.floors_due_horizon])
def test_each_pass_that_does_not_floor_gets_its_rows_horizon_text(actor):
    assert refusal(DUE_HORIZON, actor) == EXPECTED_DUE_HORIZON[actor.refusal_wording]


@pytest.mark.parametrize("actor", [a for a in PassActor if a.refusal_wording is RefusalWording.REVIEW])
@pytest.mark.parametrize("fields", [PAST_DATE, TODAY_DATE, DUE_HORIZON])
def test_a_review_actor_never_gets_the_discard_text(actor, fields):
    text = refusal(fields, actor)
    assert "DISCARD" not in text and "candidate" not in text and "history" not in text
    assert REVIEW_WAY_OUT in text


def test_the_review_and_the_owner_read_the_review_wording():
    assert {a for a in PassActor if a.refusal_wording is RefusalWording.REVIEW} == {
        PassActor.REVIEW, PassActor.OWNER, PassActor.TIMEOUT,  # P2 revised: TIMEOUT takes the owner's row
    }


def test_the_review_note():
    assert REVIEW_REPORTED_DATE_NOTE == (
        "reported_date is kept by the system in this pass (it moves only on evidence or the owner's answer); "
        "expiry horizons count from now"
    )

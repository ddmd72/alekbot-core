"""Builders for the profile-question tests (fact profile RFC §4.7): the open point of each kind and a profile question in
any status. The invented cast and places only: Rome and Porto, Alex and Jamie Doe, Alex's start at Acme."""
from datetime import datetime, timedelta, timezone
from typing import Any

from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.messaging import MessageRef
from src.domain.profile_answers import date_point_key, person_point_key, place_point_key
from src.domain.profile_open_point import OpenPointOption, ProfileOpenPoint

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REF = MessageRef(platform="slack", channel_id="D1", message_id="1729760000.000200")
LINEAGE = "lin-1"

ROME_FACT = "Alex has a trip to Rome"
PORTO_FACT = "Hanna lives in Porto"
DOE_FACT = "Doe has a trip to Lisbon"
ACME_FACT = "Alex starts at Acme on 2026-10-01"


def rome_point() -> ProfileOpenPoint:
    return ProfileOpenPoint(
        key=place_point_key("Rome", ""), field="places", printed="Rome", city="Rome", reason="ambiguous",
        options=(OpenPointOption(id="pick1", label="Rome, Italy", value="1000"),
                 OpenPointOption(id="pick2", label="Rome, United States", value="2000")))


def porto_point() -> ProfileOpenPoint:
    return ProfileOpenPoint(
        key=place_point_key("Porto", "Portugal"), field="places", printed="Porto, Portugal", city="Porto",
        country="Portugal", reason="not_in_atlas")


def doe_point() -> ProfileOpenPoint:
    return ProfileOpenPoint(
        key=person_point_key(LINEAGE, "Doe"), field="people", printed="Doe", lineage_id=LINEAGE, mention="Doe",
        options=(OpenPointOption(id="pick1", label="Alex Doe", value="Alex Doe"),
                 OpenPointOption(id="pick2", label="Jamie Doe — daughter", value="Jamie Doe")))


def acme_point() -> ProfileOpenPoint:
    return ProfileOpenPoint(
        key=date_point_key(LINEAGE), field="time_points", printed="on 2026-10-01", lineage_id=LINEAGE,
        mention="on 2026-10-01")


_KINDS = {
    "place": (rome_point, ROME_FACT),
    "person": (doe_point, DOE_FACT),
    "date": (acme_point, ACME_FACT),
}


def profile_question(kind: str = "place", status: QuestionStatus = QuestionStatus.QUEUED, **over: Any,
                     ) -> FactReviewQuestion:
    """A profile question of `kind` in `status`, with exactly the fields that status needs (as
    `review_rule_builders.question` does for a lifecycle one)."""
    point, quoted = _KINDS[kind]
    asked_at = over.pop("asked_at", NOW - timedelta(days=3))
    fields: dict[str, Any] = dict(
        question_id=f"pq-{kind}", account_id="acc", user_id="u1", fact_id=f"f-{kind}", quoted_text=quoted,
        fallback="none", origin="profile", question=kind, open_point=point(), queue_key=NOW - timedelta(days=1),
        queued_at=NOW - timedelta(days=1),
    )
    if status is not QuestionStatus.QUEUED:
        fields.update(session_id="u1:D1", appended_at=asked_at - timedelta(minutes=1), status=status)
    if status in (QuestionStatus.ASKED, QuestionStatus.UNANSWERED, QuestionStatus.CLOSED):
        fields.update(message_ref=REF, asked_at=asked_at)
    if status is QuestionStatus.PENDING and over.pop("posted", False):
        fields.update(message_ref=REF)
    fields.update(over)
    if status is QuestionStatus.CLOSED:
        fields.update(status=QuestionStatus.ASKED)
        return FactReviewQuestion(**fields).closed(ClosedBy.RESOLVED_ELSEWHERE, NOW)
    return FactReviewQuestion(**fields)

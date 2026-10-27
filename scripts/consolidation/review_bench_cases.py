"""The cases of the two memory-review benches (Facts S4 plan T12b, §8 benches 6 and 7), as data and pure builders.

No I/O here and nothing is built at import. The review bench's cases are facts the live store lacks (put in the kit's
overlay, never written) and questions about them; the answer bench's cases are conversation windows for Stage 1. Every
history shape is the production function's output (`review_frame`, `question_text`, `reply_part`, `not_delivered_frame`),
so a changed shape changes the bench with it. All examples come from the invented universe.

A fact of the review bench has no vector: no live search finds it and it finds no neighbour, so what the model decides is
decided from its own line and its batch-mates, not from the owner's real memory. A case's `expect` is what the owner reads
the result against (gate G6); only the answer bench's expectations are judged by code (gate G4).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.entities import ExpiryKind, FactDomain, FactEntity, FactState, FactType, TemporalClass
from src.domain.fact_expiry import Expiry
from src.domain.fact_lifecycle import compute_expiry
from src.domain.fact_review_frame import (
    NOT_DELIVERED_REPLY,
    not_delivered_frame,
    question_text,
    reply_part,
    review_frame,
)
from src.domain.fact_review_language import CANONICAL_LABELS, LABEL_KEYS, fact_question_message
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_rules import queued_question
from src.domain.ui_messages import UIMessage
from src.locales.uk import UI_STRINGS as _UK_STRINGS

SEED_NAMESPACE = "alekbot-review-bench"
STALE_DAYS_AGO = 2           # the stale cases went stale this long before the run
REPLY_STALE_DAYS_AGO = 5     # a reply case: stale, queued, asked and answered in the days before the run


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ the review bench

@dataclass(frozen=True)
class FactCase:
    """A fact the review bench stages stale. `reported` / `ttl_days` fix its horizon (`expires_at = reported + ttl_days`),
    and `reply` is the owner's answer in words on the question asked about it (None: no question)."""

    key: str
    text: str
    domain: FactDomain
    fact_type: FactType
    reported: datetime
    ttl_days: int
    expect: str
    reply: Optional[str] = None

    @property
    def expires_at(self) -> datetime:
        return compute_expiry(Expiry(ExpiryKind.HORIZON, days=self.ttl_days), self.reported, "UTC")["expires_at"]

    @property
    def fact_id(self) -> str:
        """Stable across runs and machines, so two runs of the bench compare fact by fact."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{SEED_NAMESPACE}/{self.key}"))


def stale_cases(now: datetime) -> Tuple[FactCase, ...]:
    """The facts the janitor would have staled, with no question. The plan's date is fixed (it is in the text); the others
    are dated from `now`."""
    return (
        FactCase(
            "plan_date_passed", "Alex will give a talk at the Hamburg conference on 15 October 2026.",
            FactDomain.WORK, FactType.STATE, _utc(2026, 9, 10), 35,
            "a horizon plan whose day passed and that nothing decides: asked with question happened (texts happened, "
            "not_happened, unknown; fallback history) or rewritten as history on evidence; never archived, never a "
            "date_passed history (its text reports no outcome)",
        ),
        FactCase(
            "revive_without_basis", "Alex trains at the Iron Temple gym.",
            FactDomain.HEALTH, FactType.STATE, now - timedelta(days=100), 30,
            "nothing in front of the model decides it: asked, not revived",
        ),
        FactCase(
            "duplicate_a", "Alex is recovering from knee surgery.",
            FactDomain.HEALTH, FactType.STATE, _utc(2026, 10, 6), 10,
            "the two say one thing: decided together (one closed in favour of the other) or both asked; never both revived",
        ),
        FactCase(
            "duplicate_b", "The user had knee surgery on 2026-10-05 and is recovering.",
            FactDomain.HEALTH, FactType.STATE, _utc(2026, 10, 7), 10,
            "the same case as duplicate_a",
        ),
        FactCase(
            "undecidable", "Alex is planning a Prague trip with a hotel budget of about 800 euros.",
            FactDomain.PROJECT, FactType.STATE, now - timedelta(days=120), 60,
            "no evidence either way: asked, not guessed",
        ),
    )


def reply_cases(now: datetime) -> Tuple[FactCase, ...]:
    """The facts whose question was asked and answered in words, the answer not yet read by the review."""
    reported = now - timedelta(days=120)
    return (
        FactCase(
            "reply_unrelated", "Alex takes the pills daily.", FactDomain.HEALTH, FactType.STATE, reported, 90,
            "the reply is about the car service: no answer to this question, nothing decided from it",
            reply="Can we move the car service to Thursday?",
        ),
        FactCase(
            "reply_still_true", "Alex walks daily in the park, now with the dog.", FactDomain.HEALTH, FactType.STATE,
            reported, 90, "revived on the reply (basis reply, the quote found in the reply)",
            reply="yes, still the case",
        ),
        FactCase(
            "reply_stopped", "Alex attends pottery classes.", FactDomain.PREFERENCE, FactType.STATE, reported, 90,
            "archived on the reply with a history fact in the past tense saying it ended in October",
            reply="stopped in October",
        ),
    )


def fact_entity(case: FactCase, account_id: str, user_id: str) -> FactEntity:
    """The case's fact as a stored current document (the kit stages it stale): no vector, a horizon that has run out."""
    return FactEntity(
        id=case.fact_id, account_id=account_id, created_by_user_id=user_id, lineage_id=f"L-{case.fact_id}",
        text=case.text, type=case.fact_type, domain=case.domain, temporal_class=TemporalClass.DYNAMIC,
        state=FactState.CURRENT, is_current=True, reported_date=case.reported, created_at=case.reported,
        valid_from=case.reported, last_updated=case.reported, expiry_kind=ExpiryKind.HORIZON, ttl_days=case.ttl_days,
        expires_at=case.expires_at,
    )


def stale_moment(case: FactCase, now: datetime) -> datetime:
    """When the case went stale; its horizon must have run out by then or the bench has no stale fact to stage."""
    moment = now - timedelta(days=REPLY_STALE_DAYS_AGO if case.reply is not None else STALE_DAYS_AGO)
    if case.expires_at > moment:
        raise ValueError(f"the case {case.key} expires on {case.expires_at.date()}: it was not due {moment.date()}")
    return moment


# ------------------------------------------------------------------------------------------------ the answer bench

# What Stage 1 must do with a window (gate G4).
NO_WRITE = "no_write"
ONE_CREATE = "one_create"

QUOTED = "Alex trains at the FitLife gym on Main Street."
QUOTED_UK = "Алекс тренується в спортзалі FitLife на Мейн-стріт."   # the translation the review pass would store for QUOTED
ECHOED = "Based on our history, you weigh 64 kg."
UK_LABELS = {key: _UK_STRINGS[key] for key in LABEL_KEYS}              # the uk UI strings of the review's texts


@dataclass(frozen=True)
class AnswerCase:
    """A conversation window for Stage 1 and the rule it is judged by. `contains` is the text the one created fact must
    hold (ONE_CREATE)."""

    name: str
    window: Tuple[Tuple[str, str], ...]     # (role, text) in order; the history's own shapes
    rule: str
    contains: Optional[str] = None
    question_id: Optional[str] = None       # the question the window's frame and reply parts name


def question_pair(question_id: str, quoted: str = QUOTED) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    """The two history parts a sent question leaves: the frame, and the question as the model part (the production
    shapes, from a question built the production way)."""
    question = _question(question_id, quoted)
    return ("user", review_frame(question_id)), ("model", question_text(fact_question_message(question, CANONICAL_LABELS)))


def _question(question_id: str, quoted: str):  # type: ignore[no-untyped-def]
    now = datetime.now(timezone.utc)
    stale = FactEntity(
        account_id="acc", created_by_user_id="u", lineage_id="L", text=quoted, type=FactType.STATE,
        state=FactState.STALE, stale_since=now,
    )
    return queued_question(stale, question_id, "u", "revive", at=now)


def translated_question_pair(question_id: str) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    """The two history parts a question leaves when the owner's message language is Ukrainian: the frame, and the
    question with the uk texts and the stored fact (`QUOTED`) shown in its translation (`QUOTED_UK`)."""
    question = _question(question_id, QUOTED)
    translation = QuoteTranslation("uk", quote_digest(QUOTED), QUOTED_UK)
    message = fact_question_message(question, UK_LABELS, translation, "uk")
    return ("user", review_frame(question_id)), ("model", question_text(message))


def answer_cases() -> Tuple[AnswerCase, ...]:
    """The Stage 1 cases of bench 6. Question ids are invented and differ per case."""
    def pair(n: int) -> Tuple[str, Tuple[Tuple[str, str], Tuple[str, str]]]:
        question_id = f"{n:02d}c4e1a7b92d4f3e8a6b05d1c7e9f3a2b"
        return question_id, question_pair(question_id)

    q1, p1 = pair(1)
    q2, p2 = pair(2)
    q3, p3 = pair(3)
    q4, p4 = pair(4)
    q5, p5 = pair(5)
    q6, p6 = pair(6)
    q7, p7 = pair(7)
    q9 = f"{9:02d}c4e1a7b92d4f3e8a6b05d1c7e9f3a2b"
    acknowledged = ("model", CANONICAL_LABELS[UIMessage.MEMORY_ANSWER_NOTED.value])
    return (
        AnswerCase("digit_reply", (*p1, ("user", reply_part(q1, "2")), acknowledged), NO_WRITE, question_id=q1),
        AnswerCase(
            "words_still", (*p2, ("user", reply_part(q2, "yes, still the case")), acknowledged), NO_WRITE, question_id=q2,
        ),
        AnswerCase(
            "plain_digit_later",
            (*p3, ("user", "What's the weather tomorrow?"), ("model", "Sunny, around 24°C."), ("user", "2")), NO_WRITE,
            question_id=q3,
        ),
        AnswerCase(
            "correction", (*p4, ("user", reply_part(q4, "3 — it's Iron Temple now")), acknowledged), ONE_CREATE,
            contains="Iron Temple", question_id=q4,
        ),
        AnswerCase("system_frame_only", p5, NO_WRITE, question_id=q5),
        AnswerCase(
            "unrelated_chat", (*p6, ("user", "Thanks, that is all for now."), ("model", "You're welcome.")), NO_WRITE,
            question_id=q6,
        ),
        AnswerCase(
            "not_delivered",
            (*p7, ("user", not_delivered_frame(q7)), ("model", NOT_DELIVERED_REPLY)), NO_WRITE, question_id=q7,
        ),
        AnswerCase("echo_control", (("model", ECHOED), ("user", "Yes, that is right.")), NO_WRITE),
        AnswerCase(
            "correction_translated_quote",
            (*translated_question_pair(q9), ("user", reply_part(q9, "3 — it's Iron Temple now")),
             ("model", UK_LABELS[UIMessage.MEMORY_REPLY_NOTED.value])),
            ONE_CREATE, contains="Iron Temple", question_id=q9,
        ),
    )


def window_messages(case: AnswerCase, now: datetime) -> List[Dict[str, object]]:
    """The case's window as Stage 1 receives a conversation: role, text, and the instant of each message (a minute apart,
    the last one a minute before `now`), the kit's `created_at` included."""
    start = now - timedelta(minutes=len(case.window) + 1)
    out: List[Dict[str, object]] = []
    for index, (role, text) in enumerate(case.window):
        stamp = (start + timedelta(minutes=index)).timestamp()
        out.append({"role": role, "text": text, "timestamp": stamp, "created_at": stamp})
    return out


def judge(case: AnswerCase, writes: Sequence[Dict[str, str]]) -> Tuple[bool, str]:
    """Whether the Stage 1 writes of one case match its rule, and why not. `writes` are the written rows of the `stage1`
    pass as `{"action", "text"}` (the stored text of the fact each row wrote)."""
    if case.rule == NO_WRITE:
        return (not writes, "no fact written" if not writes else f"{len(writes)} fact write(s) where none belongs")
    created = [w for w in writes if w["action"] == "create"]
    if len(writes) != 1 or len(created) != 1:
        return False, f"{len(writes)} write(s), {len(created)} of them creates: one create and nothing else belongs"
    holds = (case.contains or "").casefold() in created[0]["text"].casefold()
    return holds, "one fact about the true value" if holds else "the one created fact does not hold the true value"

"""M7 (Facts S4, T7): each rule sentence a consolidation or review prompt states is pinned to the domain rule its refusal uses.

The sentences are found by content, not by line number: the Stage 2a cluster message, the Stage 2b pin line, the expiry
schema of the conversation passes, the review's horizon wording, and the matching sentences of `CONSOLIDATION_LIFECYCLE`.
A sentence that restates a number or a name is asserted to be built from the constant, or equal to it; a sentence that
states a rule is asserted against the domain function that refuses or applies it."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from src.agents.consolidation_agent import (
    _BASIS_SCHEMA,
    _COUNT_WORDS_DESCRIPTION,
    _EXPIRY_SCHEMA,
    _REVIEW_COUNT_WORDS_DESCRIPTION,
    _REVIEW_EXPIRY_SCHEMA,
    _REVIEW_UPDATE_SCHEMA,
    ConsolidationAgent,
)
from src.domain.entities import ExpiryKind, FactDomain, FactState, FactType
from src.domain.exceptions import FactBornDueError, FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE, EXPIRY_SHAPES, Expiry, parse_expiry
from src.domain.fact_lifecycle import (
    MIN_REVIEW_HORIZON_DAYS, check_pin, compute_expiry, expiry_pinned, short_review_horizon,
)
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import ARCHIVE_NEEDS_REPLY, DATED_PLAN_HISTORY, REVIVE_NEEDS_BASIS, basis_refusal
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_write_context import (
    REVIEW_REPORTED_DATE_NOTE, FactWriteContext, decide_expiry, reported_date_for_merge, reported_date_for_new,
    reported_date_for_update, review_horizon,
)
from src.domain.pass_actor import PassActor
from tests.support import prompt_vocabulary as pv
from tests.support.review_rule_builders import NOW, fact, question
from tests.unit.domain.review_p2_texts import TEXTS

UTC = timezone.utc
TODAY = NOW.date()
REPORTED = NOW - timedelta(days=400)
LIFECYCLE = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_LIFECYCLE").read_text(encoding="utf-8"))["content"]


def _flat(text: str) -> str:
    """The text with its line breaks and runs of spaces read as one space (the messages wrap their sentences)."""
    return " ".join(text.split())


CLUSTER = _flat(ConsolidationAgent._build_cluster_message([]))
RULEBOOK = _flat(ConsolidationAgent._build_directive_review_message([], 15))
SHAPES = re.findall(r"\{[^}]*\}", EXPIRY_SHAPES)   # the three shapes the domain names


def _ctx(actor: PassActor) -> FactWriteContext:
    return FactWriteContext.for_pass(actor, "UTC", (NOW - timedelta(hours=1), NOW))


def _refusal(call) -> str:
    with pytest.raises(FactBornDueError) as raised:
        call()
    return _flat(str(raised.value))


# ------------------------------------------------------------------------------------------ the conversation passes' schema

def test_the_expiry_schema_offers_the_domain_kinds_in_the_domain_shapes():
    assert set(_EXPIRY_SCHEMA["properties"]["kind"]["enum"]) == {k.value for k in ExpiryKind}
    assert len(SHAPES) == 3
    for shape in SHAPES:
        assert shape in _EXPIRY_SCHEMA["description"], shape
        assert parse_expiry(_shape_to_dict(shape)).kind.value in shape


def _shape_to_dict(shape: str) -> dict:
    """`{kind: date, date: YYYY-MM-DD}` as the model would send it, with a real value in each placeholder."""
    values = {"YYYY-MM-DD": "2030-01-31", "N": 30}
    pairs = [p.split(": ") for p in shape.strip("{}").split(", ")]
    return {key: values.get(value, value) for key, value in pairs}


def test_the_pinned_sentence_names_the_facts_the_domain_pins_to_none():
    sentence = "Directives, principles and mindset facts: none."
    assert sentence in _EXPIRY_SCHEMA["description"]
    assert expiry_pinned(FactDomain.AGENT_DIRECTIVE, FactType.STATE, [])
    assert expiry_pinned(FactDomain.PREFERENCE, FactType.PRINCIPLE, [])
    assert expiry_pinned(FactDomain.PREFERENCE, FactType.STATE, ["Mindset"])
    assert not expiry_pinned(FactDomain.PREFERENCE, FactType.STATE, [])
    assert check_pin(None, True) == EXPIRY_NONE
    with pytest.raises(FactExpiryError) as raised:
        check_pin(Expiry(ExpiryKind.HORIZON, days=30), True)
    assert str(raised.value).lower().startswith("directives, principles and mindset facts take {kind: none}")


def test_a_horizon_of_the_conversation_passes_counts_from_when_the_owner_said_it():
    assert "review if the owner has not said it again within N days" in _EXPIRY_SCHEMA["description"]
    assert _EXPIRY_SCHEMA["properties"]["days"]["description"] == "with kind horizon: days counted from when the owner said it"
    fields = compute_expiry(Expiry(ExpiryKind.HORIZON, days=30), REPORTED, "UTC")
    assert fields["expires_at"] == REPORTED + timedelta(days=30)


def test_a_date_is_the_last_day_the_fact_holds():
    assert "{kind: date, date: YYYY-MM-DD} the last day it holds" in _EXPIRY_SCHEMA["description"]
    fields = compute_expiry(Expiry(ExpiryKind.DATE, on=date(2030, 1, 31)), REPORTED, "UTC")
    assert fields["expires_at"] == datetime(2030, 2, 1, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ Stage 2a cluster message

def test_stage_2a_a_past_date_sent_back_is_refused_and_the_way_out_is_the_domains():
    sentence = ("A past `date` is refused if you send it back: rewrite that fact as what happened with `{kind: none}`, "
                "or give a future date if the plan still holds; do not drop it because its date passed.")
    assert sentence in CLUSTER
    text = _refusal(lambda: decide_expiry(
        Expiry(ExpiryKind.DATE, on=TODAY - timedelta(days=3)), REPORTED, NOW, _ctx(PassActor.STAGE2A)))
    assert "do not drop the fact because its date passed" in text
    assert "with {kind: none}, or give a future date if the plan still holds" in text


def test_stage_2a_a_date_of_today_asks_for_a_short_horizon():
    assert "A `date` of today asks for a short horizon." in CLUSTER
    text = _refusal(lambda: decide_expiry(Expiry(ExpiryKind.DATE, on=TODAY), REPORTED, NOW, _ctx(PassActor.STAGE2A)))
    assert "a plan for today is not over yet: give a short horizon" in text


def test_stage_2a_a_horizon_counts_from_reported_which_the_pass_cannot_change():
    assert "A horizon counts from `reported`; this pass cannot change `reported`." in CLUSTER
    ctx = _ctx(PassActor.STAGE2A)
    assert not ctx.model_dates
    kept, _ = reported_date_for_update(ctx, NOW, REPORTED, True, NOW)
    assert kept == REPORTED
    fields, _ = decide_expiry(Expiry(ExpiryKind.HORIZON, days=500), REPORTED, NOW, ctx)
    assert fields["expires_at"] == REPORTED + timedelta(days=500)


def test_stage_2a_a_split_takes_its_date_from_derived_from_and_a_merge_from_its_sources():
    assert "give each create_fact `derived_from: [\"<the split fact's id>\"]`;" in CLUSTER
    assert "a merge takes its date from its sources by itself." in CLUSTER
    ctx, older, newer = _ctx(PassActor.STAGE2A), REPORTED, REPORTED + timedelta(days=9)
    assert reported_date_for_new(ctx, None, [older, newer], NOW, derived_named=2)[0] == newer
    assert reported_date_for_merge(ctx, None, [older, newer], NOW)[0] == newer


def test_the_word_limit_is_one_number_in_every_prompt_that_states_it():
    numbers = {
        "cluster": re.findall(r"no fact may exceed (\d+) words", CLUSTER),
        "count_words": re.findall(r"≤(\d+) words", _COUNT_WORDS_DESCRIPTION),
        "review count_words": re.findall(r"≤(\d+) words", _REVIEW_COUNT_WORDS_DESCRIPTION),
        "lifecycle": re.findall(r"the (\d+)-word limit", LIFECYCLE),
    }
    assert all(len(found) == 1 for found in numbers.values()), numbers
    assert len({found[0] for found in numbers.values()}) == 1, numbers


# ----------------------------------------------------------------------------------------------- Stage 2b pin line

def test_stage_2b_merges_a_directive_with_the_one_expiry_the_domain_allows():
    line = 'expiry: {kind: "none"} - directives are pinned to none'
    assert line in RULEBOOK
    assert parse_expiry({"kind": "none"}) == EXPIRY_NONE
    assert expiry_pinned(FactDomain.AGENT_DIRECTIVE, FactType.STATE, []) and check_pin(None, True) == EXPIRY_NONE


# ------------------------------------------------------------------------------------------------- the review's horizon

def test_the_review_schema_states_the_minimum_horizon_from_the_constant():
    assert f"(N ≥ {MIN_REVIEW_HORIZON_DAYS})" in _REVIEW_EXPIRY_SCHEMA["description"]
    assert _REVIEW_EXPIRY_SCHEMA["properties"]["days"]["description"].startswith(
        f"with kind horizon: N ≥ {MIN_REVIEW_HORIZON_DAYS}, the days from now")


def test_a_review_horizon_under_the_minimum_is_refused_and_the_minimum_counts_from_now():
    ctx = _ctx(PassActor.REVIEW)
    short = MIN_REVIEW_HORIZON_DAYS - 1
    with pytest.raises(FactBornDueError) as raised:
        review_horizon(Expiry(ExpiryKind.HORIZON, days=short), REPORTED, NOW, ctx)
    assert str(raised.value) == str(short_review_horizon(short))
    fields, _ = review_horizon(Expiry(ExpiryKind.HORIZON, days=MIN_REVIEW_HORIZON_DAYS), REPORTED, NOW, ctx)
    assert fields["expires_at"] >= NOW + timedelta(days=MIN_REVIEW_HORIZON_DAYS)


def test_the_review_says_reported_date_is_the_systems_and_horizons_count_from_now():
    updates = _REVIEW_UPDATE_SCHEMA["parameters"]["properties"]["updates"]["description"]
    assert "reported_date is kept by the system; review horizons count from now." in updates
    assert "reported_date" not in _REVIEW_UPDATE_SCHEMA["parameters"]["properties"]["updates"]["properties"]
    assert REVIEW_REPORTED_DATE_NOTE.startswith("reported_date is kept by the system")
    assert REVIEW_REPORTED_DATE_NOTE.endswith("expiry horizons count from now")
    _, note = reported_date_for_update(_ctx(PassActor.REVIEW), NOW, REPORTED, False, NOW)
    assert note == REVIEW_REPORTED_DATE_NOTE


# ------------------------------------------------------------------------------------------- CONSOLIDATION_LIFECYCLE

def test_the_lifecycle_horizon_sentence_is_the_schemas_with_the_constant():
    shape, rule = "{kind: horizon, days: N}", f"means review again in N days (N ≥ {MIN_REVIEW_HORIZON_DAYS})"
    assert f"Horizons count from now: {shape} {rule}." in LIFECYCLE
    assert f"a horizon counts from now: `{shape}` {rule}" in _REVIEW_EXPIRY_SCHEMA["description"]


def test_the_lifecycle_expiry_shapes_are_the_domains():
    assert EXPIRY_SHAPES in LIFECYCLE
    assert "reported_date is kept by the system: do not send it." in LIFECYCLE


def _calls_line(action: str) -> str:
    return re.search(rf'^\s*{action}: "([^"]*)"', LIFECYCLE, re.MULTILINE).group(1)


def _named_kinds(action: str) -> set:
    """The basis kinds the LIFECYCLE `calls` line of `action` writes in `basis=<…>`."""
    inside = re.search(r"basis=<([^>]*)>", _calls_line(action)).group(1)
    return set(re.findall(r"evidence|reply|date_passed", inside))


_UPDATES = {
    "revive": ParsedFactUpdate(state=FactState.CURRENT),
    "rewrite": ParsedFactUpdate(content="Drinks green tea daily", state=FactState.CURRENT),
    "archive": ParsedFactUpdate(state=FactState.ARCHIVED),
    "invalidate": ParsedFactUpdate(state=FactState.INVALIDATED),
    # P2 revised (P2-1): the token's `history` call, a rewrite as history
    "history": ParsedFactUpdate(
        content="The user drank green tea until Oct 2026.", state=FactState.CURRENT, expiry=EXPIRY_NONE, as_history=True,
    ),
}
_REPLIED = question(QuestionStatus.ASKED, reply_text="Yes, I still drink it every morning", reply_at=NOW - timedelta(hours=5))
_EVIDENCE = fact("ev", lineage_id="L-ev", state=FactState.CURRENT)
_PAST_PLAN = dict(expiry_kind=ExpiryKind.DATE, ttl_days=None, expires_at=datetime(2026, 10, 1, tzinfo=UTC))


def _basis_refusal(action: str, kind: str):
    subject = fact(**_PAST_PLAN) if kind == "date_passed" else fact()
    basis = {
        "evidence": ReviewBasis("evidence", evidence_fact_id="ev"),
        "reply": ReviewBasis("reply", question_id="q1", quote="still drink it"),
        "date_passed": ReviewBasis("date_passed"),
    }[kind]
    return basis_refusal(
        subject, _UPDATES[action], basis, named_fact=_EVIDENCE if kind == "evidence" else None,
        question=_REPLIED if kind == "reply" else None, made_current=frozenset(), history=None, today=TODAY, tz="UTC",
    )


@pytest.mark.parametrize("action", sorted(_UPDATES))
def test_the_bases_the_lifecycle_token_gives_each_change_are_the_ones_the_domain_accepts(action):
    named = _named_kinds(action)
    assert named, action
    for kind in ("evidence", "reply", "date_passed"):
        refusal = _basis_refusal(action, kind)
        assert (refusal is None) == (kind in named), (action, kind, refusal)


def test_the_schema_says_the_same_bases_as_the_lifecycle_token():
    description = _BASIS_SCHEMA["description"]
    assert "A revive, a rewrite and an invalidation need evidence or reply" in description
    # P2 revised (P2-2): an archive needs the owner's reply; a history rewrite takes any of the three kinds
    assert "an archive needs reply" in description
    assert "a history rewrite (as_history) takes evidence, reply or date_passed" in description
    assert _named_kinds("revive") == _named_kinds("rewrite") == _named_kinds("invalidate") == {"evidence", "reply"}
    assert _named_kinds("archive") == {"reply"}
    assert _named_kinds("history") == {"evidence", "reply", "date_passed"}


def test_a_revive_with_nothing_behind_it_is_refused_as_the_lifecycle_token_says():
    assert "Never revive, archive or invalidate a fact nothing in front of you decides." in LIFECYCLE
    refusal = basis_refusal(fact(), _UPDATES["revive"], None, named_fact=None, question=None, made_current=frozenset(),
                            history=None, today=TODAY, tz="UTC")
    assert refusal == REVIVE_NEEDS_BASIS and "ask the owner" in REVIVE_NEEDS_BASIS
    assert "When unsure, ask: call ask_owner." in LIFECYCLE


def test_a_supersede_takes_no_basis_but_a_current_successor():
    assert "it is the reason, so send no basis" in _calls_line("supersede")
    update = ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="ev")
    assert basis_refusal(fact(), update, None, named_fact=_EVIDENCE, question=None, made_current=frozenset(),
                         history=None, today=TODAY, tz="UTC") is None
    closed = fact("ev", lineage_id="L-ev", state=FactState.ARCHIVED)
    assert basis_refusal(fact(), update, None, named_fact=closed, question=None, made_current=frozenset(),
                         history=None, today=TODAY, tz="UTC") is not None


def test_history_goes_with_an_ended_reply_and_never_with_a_dated_plan():
    # P2 revised (P2-1, C5): a date fact is not archived beside a history fact; it is rewritten as history on the reply
    assert "A fact of expiry kind date is not archived: rewrite it as history (as_history) on basis reply." in LIFECYCLE
    archive, reply = _UPDATES["archive"], ReviewBasis("reply", question_id="q1", quote="still drink it")
    standing = basis_refusal(fact(), archive, reply, named_fact=None, question=_REPLIED, made_current=frozenset(),
                             history="He stopped drinking green tea in October 2026.", today=TODAY, tz="UTC")
    assert standing is None
    plan = basis_refusal(fact(**_PAST_PLAN), archive, reply, named_fact=None, question=_REPLIED,
                         made_current=frozenset(), history="The trip took place.", today=TODAY, tz="UTC")
    assert plan == DATED_PLAN_HISTORY
    rewritten = basis_refusal(fact(**_PAST_PLAN), _UPDATES["history"], reply, named_fact=None, question=_REPLIED,
                              made_current=frozenset(), history=None, today=TODAY, tz="UTC")
    assert rewritten is None


# ---------------------------------------------------------------------------------- T7 fix round (review S4, S6)

async def test_the_word_limit_every_prompt_states_is_the_limit_count_words_reports():
    """S4: the number the prompts state is the one the code's `count_words` reports (no domain constant yet, F1)."""
    import json
    from unittest.mock import AsyncMock

    from src.ports.llm_port import ToolCall
    from src.ports.fact_management_port import FactManagementPort
    from tests.unit.agents.test_consolidation_agent import _make_agent

    agent, _, _, _ = _make_agent(fact_management=AsyncMock(spec=FactManagementPort))
    [result] = await agent._execute_fact_management_tools(
        [ToolCall(name="count_words", args={"text": "one two"})], "u1", "acc", agent._write_context("stage1"))
    limit = json.loads(result.result_str)["limit"]
    policies = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_POLICIES").read_text(encoding="utf-8"))["content"]
    taxonomy = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_TAXONOMY").read_text(encoding="utf-8"))["content"]
    stated = {
        "cluster": re.findall(r"no fact may exceed (\d+) words", CLUSTER),
        "count_words": re.findall(r"≤(\d+) words", _COUNT_WORDS_DESCRIPTION),
        "review count_words": re.findall(r"≤(\d+) words", _REVIEW_COUNT_WORDS_DESCRIPTION),
        "lifecycle": re.findall(r"the (\d+)-word limit", LIFECYCLE),
        "Size_Triggers_Review": re.findall(r"Existing facts retrieved from DB that exceed (\d+) words", policies),
        "Size_Triggers_Review trigger": re.findall(r"existing_fact\.word_count > (\d+) AND", policies),
        "Directive_Maintenance": re.findall(r"Self-contained atomic rule, max (\d+) words", taxonomy),
    }
    assert all(len(found) == 1 for found in stated.values()), stated
    assert {int(found[0]) for found in stated.values()} == {limit}, (limit, stated)
    over = await agent._execute_fact_management_tools(
        [ToolCall(name="count_words", args={"text": " ".join(["w"] * (limit + 1))})], "u1", "acc",
        agent._write_context("stage1"))
    assert json.loads(over[0].result_str)["within_limit"] is False


def _ask(fallback, *, kind=ExpiryKind.HORIZON, fallback_date=None, noise=False, open_question=None, question="still_true"):
    from src.domain.fact_review_rules import ask_refusal

    subject = fact(**_PAST_PLAN) if kind is ExpiryKind.DATE else fact()
    return ask_refusal(subject, fallback, fallback_date, noise, open_question, NOW, "UTC", question=question,
                       texts=TEXTS[question])


ASKING = " ".join(re.search(r"asking: \[(.*?)\n    \]", LIFECYCLE, re.DOTALL).group(1).split())


# P2 revised (P2-3): history is the fallback of happened, renewed and paid; revive of still_true; archive only with
# noise; a date fact is asked happened, renewed or paid, or still_true with archive + noise; fallback_date is retired.
@pytest.mark.parametrize("case,refused", [
    (dict(fallback="revive"), False),
    (dict(fallback="revive", fallback_date=TODAY - timedelta(days=3)), True),
    (dict(fallback="revive", noise=True), True),
    (dict(fallback="archive", kind=ExpiryKind.DATE), True),
    (dict(fallback="archive", fallback_date=TODAY - timedelta(days=3)), True),
    (dict(fallback="archive", noise=True), False),
    (dict(fallback="archive"), True),
    (dict(fallback="archive", fallback_date=TODAY), True),
    (dict(fallback="archive", fallback_date=TODAY + timedelta(days=3)), True),
    (dict(fallback="later"), True),
    (dict(fallback="archive", kind=ExpiryKind.DATE, noise=True), False),
    (dict(fallback="revive", kind=ExpiryKind.DATE), True),
    (dict(fallback="history"), True),
    (dict(fallback="history", question="happened", kind=ExpiryKind.DATE), False),
    (dict(fallback="history", question="renewed", kind=ExpiryKind.DATE), False),
    (dict(fallback="history", question="paid", kind=ExpiryKind.DATE), False),
    (dict(fallback="history", question="happened"), False),
    (dict(fallback="revive", question="happened"), True),
    (dict(fallback="revive", question="renewed"), True),
    (dict(fallback="archive", question="happened", noise=True), False),
    (dict(fallback="archive", question="paid", noise=True), True),
], ids=["revive", "revive+date", "revive+noise", "archive-date-fact", "archive-past-date", "archive-noise",
        "archive-bare", "archive-date-today", "archive-future-date", "unknown", "archive-noise-date-fact",
        "revive-date-fact", "history-still-true", "happened-date-fact", "renewed-date-fact", "paid-date-fact",
        "happened-horizon-plan", "happened-revive", "renewed-revive", "happened-archive-noise", "paid-archive-noise"])
def test_the_fallback_rule_the_prompts_state_is_the_one_ask_refusal_applies(case, refused):
    """S6: LIFECYCLE's asking lines and `_ASK_OWNER_SCHEMA`'s fallback description, against `ask_refusal`."""
    from src.agents.consolidation_agent import _ASK_OWNER_SCHEMA

    fallback = _ASK_OWNER_SCHEMA["parameters"]["properties"]["fallback"]["description"]
    assert ("history (its unknown text is written) for happened, renewed and paid; revive for still_true; archive only "
            "with noise") in fallback
    assert "fallback_date" not in _ASK_OWNER_SCHEMA["parameters"]["properties"]
    assert ("fallback is what happens if the owner does not answer: history (its unknown text is written) for happened, "
            "renewed and paid; revive for still_true; archive only with noise: true when the fact reads as noise, not as "
            "something the owner said. A fact of expiry kind date is asked as happened, renewed or paid, or as still_true "
            "with archive and noise: true.") in ASKING
    assert (_ask(**case) is not None) == refused, case


@pytest.mark.parametrize("asked_days_ago", [3, 20], ids=["open", "timed-out"])
def test_a_fact_whose_question_is_open_or_timed_out_is_not_asked_again(asked_days_ago):
    """S6 / S1: LIFECYCLE's "no open question" and "Do not ask it again", against `ask_refusal`."""
    q = question(QuestionStatus.ASKED, asked_at=NOW - timedelta(days=asked_days_ago))
    assert q.is_timed_out(NOW) == (asked_days_ago == 20)
    assert "one listed fact with no result in this pass and no open question" in ASKING
    assert "Do not ask it again." in ASKING
    assert "A fact whose question is open or timed out is not asked again" in LIFECYCLE
    for fallback in ("revive", "archive"):
        refusal = _ask(fallback, kind=ExpiryKind.DATE, open_question=q)
        assert refusal is not None and "leave it" in refusal


def test_a_date_fact_rewritten_as_history_on_date_passed_uses_its_own_date():
    """S6 (optional): "A fact of expiry kind date uses its own date: leave date out" against `plan_date`. P2 revised:
    date_passed backs only a history rewrite, never an archive ("It backs only a history rewrite")."""
    from src.domain.fact_review_rules import plan_date

    assert "A fact of expiry kind date uses its own date: leave date out." in LIFECYCLE
    assert "It backs only a history rewrite" in LIFECYCLE
    assert plan_date(fact(**_PAST_PLAN), ReviewBasis("date_passed"), "UTC") == date(2026, 9, 30)
    assert _basis_refusal("history", "date_passed") is None
    assert _basis_refusal("archive", "date_passed") == ARCHIVE_NEEDS_REPLY

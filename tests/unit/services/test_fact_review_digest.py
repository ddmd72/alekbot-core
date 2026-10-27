"""`FactReviewService.run`: the weekly digest and the `undigested_since` mark that makes it due (Facts S4 · T8d2, plan §3.11,
§3.17; S4-D8).

The plain service on the review world with the scripted consolidation agent of the T8b tests: the real review store,
repository, counters, session store, `FactAnswerService`, texts and owner zone over one evaluating Firestore fake. The digest
goes out through the world's recording channel (`notify_raw`); assertions are on the text it received, the stored state,
the stored claim documents and the session history. NOW is 2026-10-23 07:30 UTC (a Friday, ISO week 43), 08:30 in Lisbon."""
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import labels_digest
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_task import FactReviewTask
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.infrastructure.fact_review_scope import FactReviewScope
from tests.support.fact_kit import ACC, USER as ANSWERER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_answer_service import question_on, service as answer_service, stale_fact
from tests.unit.services.test_fact_review_selection import (
    PT_LABELS, SESSION, USER, Rig, asked, current, day_row, evidence, on, stale,
)
from tests.unit.services.test_fact_review_timeouts import timing_out

DAY = timedelta(days=1)
EN_FOOTER = "To bring one back, tell me in chat that it is still true."
EN_HEADER = "Facts I put aside this week:"
PT = {
    **PT_LABELS,
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "já não é verdade",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "nunca foi verdade",
}


def event(at: datetime, actor: str, to: str = "archived") -> dict:
    return {"at": at, "from": "current", "to": to, "actor": actor, "reason": "code-built reason"}


async def closed(world: ReviewWorld, text: str, actor: str = "review", days_ago: float = 1,
                 state: FactState = FactState.ARCHIVED, **over: Any) -> str:
    """A fact set aside `days_ago` days before the world's now, its last trail entry under `actor`."""
    at = world.now - timedelta(days=days_ago)
    return await world.seed(text, state=state, valid_to=at, lifecycle_events=[event(at, actor, state.value)], **over)


async def due_world(**state: Any) -> ReviewWorld:
    """A world whose owner has a channel and an account due for a digest: something was set aside three days ago."""
    world = ReviewWorld()
    await world.notification_state.save_primary(USER, "slack", "D1")
    state.setdefault("undigested_since", NOW - 3 * DAY)
    state.setdefault("last_digest_at", NOW - 8 * DAY)
    await world.seed_state(account_id=ACC, **state)
    return world


def digests(world: ReviewWorld) -> List[str]:
    return [text for _, _, text in world.channels.sent]


def lines_of(text: str) -> List[str]:
    return text.split("\n")


# ------------------------------------------------------------------------------------------------ the weekly digest

class TestWeekly:
    async def test_a_due_digest_lists_what_the_review_a_timeout_and_the_owner_set_aside_and_moves_the_marks(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log", "review", 1)
        await closed(world, "Alex plans a weekend in Coimbra", "timeout", 2)
        await closed(world, "Alex orders a cookbook", "owner", 3, FactState.INVALIDATED)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        assert digests(world) == [
            "\n".join([
                EN_HEADER,
                "• «Alex keeps a weight log» — no longer true",
                "• «Alex plans a weekend in Coimbra» — no longer true",
                "• «Alex orders a cookbook» — never was true",
                "",
                EN_FOOTER,
            ])
        ]
        state = await world.review_store.get_state(ACC)
        assert state.last_digest_at == NOW and state.undigested_since is None

    async def test_it_goes_to_the_owners_channel_and_never_into_the_history(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")
        await world.session_store.append_messages_batch(SESSION, [], owner_id=USER, raise_on_error=True)
        before = await world.session_store.messages(SESSION)

        await Rig(world).run()

        assert [(platform, channel) for platform, channel, _ in world.channels.sent] == [("slack", "D1")]
        assert await world.session_store.messages(SESSION) == before == []

    async def test_it_is_sent_as_literal_text_so_a_quoted_fact_is_shown_and_never_obeyed(self):
        # T12c A4/R1: the digest quotes stored fact text, which the bot did not write; it goes through `send_literal`
        # only, so `<!channel>` reaches the platform's literal path as written, never its markdown send.
        world = await due_world()
        await closed(world, "Alex asked the <!channel> to bring cake & tea")

        await Rig(world).run()

        [text] = digests(world)
        assert "• «Alex asked the <!channel> to bring cake & tea» — no longer true" in text
        assert world.channels.sent_literal == world.channels.sent == [("slack", "D1", text)]

    @pytest.mark.parametrize("state", [
        dict(undigested_since=None, last_digest_at=None),
        dict(undigested_since=NOW - 3 * DAY, last_digest_at=NOW - 6 * DAY),
    ], ids=["nothing_set_aside", "digest_six_days_ago"])
    async def test_it_is_not_sent_when_nothing_was_set_aside_or_the_last_digest_is_under_seven_days_old(self, state):
        world = await due_world(**state)
        await closed(world, "Alex keeps a weight log")

        await Rig(world).run()

        assert digests(world) == []
        stored = await world.review_store.get_state(ACC)
        assert (stored.last_digest_at, stored.undigested_since) == (state["last_digest_at"], state["undigested_since"])
        assert world.message_docs == {}

    async def test_the_digest_comes_due_at_exactly_seven_days_and_lists_only_what_closed_since_the_last_one(self):
        world = await due_world(last_digest_at=NOW - 7 * DAY)
        await closed(world, "Alex keeps a weight log", days_ago=6)
        await closed(world, "Alex reads at the Example Library", days_ago=8)       # told in the last digest

        await Rig(world).run()

        [text] = digests(world)
        assert "weight log" in text and "Example Library" not in text

    async def test_the_first_digest_looks_back_thirty_days(self):
        world = await due_world(last_digest_at=None)
        await closed(world, "Alex keeps a weight log", days_ago=29)
        await closed(world, "Alex reads at the Example Library", days_ago=31)

        await Rig(world).run()

        [text] = digests(world)
        assert "weight log" in text and "Example Library" not in text

    async def test_it_lists_removals_only(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")
        await world.seed("Alex walks to Main Square", lifecycle_events=[event(NOW - DAY, "review", "current")])
        await stale(world, "Alex visits Clínica Ribeira", NOW - 4 * DAY)
        await world.seed("Alex trains at the Iron Temple gym", state=FactState.SUPERSEDED, valid_to=NOW - DAY,
                         lifecycle_events=[event(NOW - DAY, "review", "superseded")])

        await Rig(world).run()

        [text] = digests(world)
        assert [line for line in lines_of(text) if line.startswith("•")] == ["• «Alex keeps a weight log» — no longer true"]

    async def test_a_fact_the_owner_ended_is_listed_as_archived_and_its_history_fact_is_not(self):
        world = await due_world(undigested_since=None, last_digest_at=NOW - 8 * DAY)
        fact = await stale_fact(world, "Alex keeps a weight log")
        await question_on(world, fact)
        await answer_service(world).apply(ACC, ANSWERER, "q1", AnswerKind.ENDED, "button", NOW - 2 * DAY)
        history = [d for d in world.docs.values() if d["text"].startswith("No longer true (the owner said so in")]
        assert len(history) == 1 and history[0]["state"] == "current"

        await Rig(world).run()

        [text] = digests(world)
        assert [line for line in lines_of(text) if line.startswith("•")] == ["• «Alex keeps a weight log» — no longer true"]

    @pytest.mark.parametrize("actor", ["review", "timeout", "owner"])
    async def test_the_reviews_a_timeouts_and_the_owners_closes_are_listed(self, actor):
        world = await due_world()
        await closed(world, "Alex keeps a weight log", actor)

        await Rig(world).run()

        assert len(digests(world)) == 1

    @pytest.mark.parametrize("actor", ["stage1", "stage2a", "stage2b", "cluster", "backstop", "email", "janitor", "cabinet"])
    async def test_any_other_actors_close_is_not_listed(self, actor):
        world = await due_world()
        await closed(world, "Alex keeps a weight log", actor)

        await Rig(world).run()

        assert digests(world) == []

    async def test_at_most_fifty_facts_newest_first(self):
        world = await due_world()
        for n in range(52):
            await closed(world, f"neutral fact {n:02d}", days_ago=1 + n / 100)

        await Rig(world).run()

        [text] = digests(world)
        bullets = [line for line in lines_of(text) if line.startswith("•")]
        assert len(bullets) == 50 and bullets[0].startswith("• «neutral fact 00»") and "fact 50" not in text


# ------------------------------------------------------------------------------------------------ one per ISO week

class TestOncePerWeek:
    async def test_a_second_run_in_the_same_iso_week_sends_nothing(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")
        await Rig(world).run()
        # The marks are lost (as by a restore) and something else is set aside: the account looks due again.
        await world.seed_state(account_id=ACC, undigested_since=NOW - DAY, last_digest_at=NOW - 8 * DAY)
        await closed(world, "Alex plans a weekend in Coimbra")

        await Rig(world).run()

        assert len(digests(world)) == 1
        assert list(world.message_docs) == [f"{USER}:digest:2026-W43"]

    async def test_the_next_weeks_digest_is_sent(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")
        await Rig(world).run()
        world.advance(days=7)
        await closed(world, "Alex plans a weekend in Coimbra", days_ago=1)
        await world.seed_state(account_id=ACC, undigested_since=world.now - DAY, last_digest_at=NOW)

        await Rig(world).run()

        assert len(digests(world)) == 2 and "Coimbra" in digests(world)[1]
        assert sorted(world.message_docs) == [f"{USER}:digest:2026-W43", f"{USER}:digest:2026-W44"]

    async def test_a_run_just_after_the_owners_midnight_belongs_to_the_owners_week_not_utcs(self):
        world = await due_world(last_digest_at=datetime(2026, 7, 1, tzinfo=timezone.utc), undigested_since=datetime(2026, 7, 2, tzinfo=timezone.utc))
        world.now = datetime(2026, 7, 12, 23, 30, tzinfo=timezone.utc)     # Sunday 23:30 UTC = Monday 00:30 in Lisbon (WEST)
        await closed(world, "Alex keeps a weight log", days_ago=1)

        await Rig(world).run()

        assert len(digests(world)) == 1
        assert list(world.message_docs) == [f"{USER}:digest:2026-W29"]       # Monday 13 July; UTC's Sunday is W28


# ------------------------------------------------------------------------------------------------ the message language

class TestLanguage:
    @pytest.mark.parametrize("language", [LanguageCode.UK, LanguageCode.EN, LanguageCode.FR, LanguageCode.ES])
    async def test_the_lines_are_the_ui_strings_of_a_fixed_language_and_the_footer_is_memory_digest_footer(self, language):
        world = await due_world()
        world.language.preferences[USER] = (language, False)
        await closed(world, "Alex keeps a weight log", "review")
        await closed(world, "Alex orders a cookbook", "owner", 2, FactState.INVALIDATED)
        text = lambda key: world.localization.get_ui_string(language, key)       # noqa: E731

        await Rig(world).run()

        assert digests(world) == ["\n".join([
            text(UIMessage.MEMORY_DIGEST_HEADER),
            f"• «Alex keeps a weight log» — {text(UIMessage.MEMORY_DIGEST_ARCHIVED)}",
            f"• «Alex orders a cookbook» — {text(UIMessage.MEMORY_DIGEST_INVALIDATED)}",
            "",
            text(UIMessage.MEMORY_DIGEST_FOOTER),
        ])]

    async def test_the_english_footer_is_the_specified_sentence(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")

        await Rig(world).run()

        assert lines_of(digests(world)[0])[-1] == EN_FOOTER == world.localization.get_ui_string(
            LanguageCode.EN, UIMessage.MEMORY_DIGEST_FOOTER)

    async def test_a_language_without_ui_strings_uses_its_checked_model_written_texts(self):
        world = await due_world(message_language="pt", labels={"pt": PT}, label_digests={"pt": labels_digest()})
        await closed(world, "Alex keeps a weight log", "review")
        await closed(world, "Alex orders a cookbook", "owner", 2, FactState.INVALIDATED)

        await Rig(world).run()

        assert digests(world) == ["\n".join([
            PT[UIMessage.MEMORY_DIGEST_HEADER.value],
            "• «Alex keeps a weight log» — já não é verdade",
            "• «Alex orders a cookbook» — nunca foi verdade",
            "",
            PT[UIMessage.MEMORY_DIGEST_FOOTER.value],
        ])]
        assert (await day_row(world)) is None

    async def test_model_texts_that_fail_the_check_fall_back_to_the_ui_language_and_are_counted(self):
        bad = {**PT, UIMessage.MEMORY_DIGEST_FOOTER.value: "Responda a [1] se ainda é verdade."}
        world = await due_world(message_language="pt", labels={"pt": bad}, label_digests={"pt": labels_digest()})
        await closed(world, "Alex keeps a weight log", "review")

        await Rig(world).run()

        assert lines_of(digests(world)[0])[-1] == EN_FOOTER
        assert (await day_row(world)).labels_fallback == 1

    async def test_texts_written_before_the_state_words_existed_are_not_used(self):
        old = {k: v for k, v in PT.items() if k not in (UIMessage.MEMORY_DIGEST_ARCHIVED.value,
                                                        UIMessage.MEMORY_DIGEST_INVALIDATED.value)}
        world = await due_world(message_language="pt", labels={"pt": old}, label_digests={"pt": "0123456789abcdef"})
        await closed(world, "Alex keeps a weight log", "review")

        await Rig(world).run()

        assert lines_of(digests(world)[0])[-1] == EN_FOOTER


# ------------------------------------------------------------------------------------------------ nothing to list, delivery

class TestMarksAndDelivery:
    async def test_nothing_to_list_sends_no_message_but_claims_the_week_and_moves_the_marks(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log", "stage2b")           # not the review's

        await Rig(world).run()

        assert digests(world) == []
        state = await world.review_store.get_state(ACC)
        assert state.last_digest_at == NOW and state.undigested_since is None
        assert list(world.message_docs) == [f"{USER}:digest:2026-W43"]

    async def test_without_a_usable_channel_nothing_is_sent_the_claim_is_released_and_the_marks_stay(self):
        world = await due_world()
        world.notification_state.primary.clear()
        await closed(world, "Alex keeps a weight log")

        assert await Rig(world).run() == {"status": "completed"}

        assert digests(world) == [] and world.message_docs == {}
        state = await world.review_store.get_state(ACC)
        assert state.last_digest_at == NOW - 8 * DAY and state.undigested_since == NOW - 3 * DAY
        await world.notification_state.save_primary(USER, "slack", "D1")       # the next run delivers it

        await Rig(world).run()

        assert len(digests(world)) == 1 and (await world.review_store.get_state(ACC)).undigested_since is None

    async def test_a_post_that_fails_is_tried_again_by_the_next_run(self):
        world = await due_world()
        world.channels.fail = True
        await closed(world, "Alex keeps a weight log")

        assert await Rig(world).run() == {"status": "completed"}
        assert world.message_docs == {} and (await world.review_store.get_state(ACC)).undigested_since == NOW - 3 * DAY
        world.channels.fail = False

        await Rig(world).run()

        assert len(digests(world)) == 1

    async def test_a_failing_read_ends_the_digest_and_not_the_run(self, monkeypatch):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("index not ready")

        monkeypatch.setattr(world.repo, "get_recently_closed", broken)

        assert await Rig(world).run() == {"status": "completed"}
        assert digests(world) == [] and world.message_docs == {}
        assert (await world.review_store.get_state(ACC)).undigested_since == NOW - 3 * DAY
        assert (await world.review_store.get_state(ACC)).last_review_at == NOW         # the run's own mark stands

    async def test_an_account_without_a_review_state_has_no_digest(self):
        world = ReviewWorld()
        await world.notification_state.save_primary(USER, "slack", "D1")
        await closed(world, "Alex keeps a weight log")

        await Rig(world).run()

        assert digests(world) == [] and world.message_docs == {}


# ------------------------------------------------------------------------------------------------ undigested_since

class TestUndigestedSince:
    """Whatever sets a fact aside marks the account, once: the first instant stays until a digest clears it."""

    async def _state(self, world: ReviewWorld):
        return await world.review_store.get_state(ACC)

    async def test_a_review_pass_that_sets_a_fact_aside_marks_the_account(self):
        # P2 revised (P2-2): evidence backs no archive; the pass sets the fact aside by an invalidation it backs, which
        # marks the account the same way (pass_set_aside)
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        proof = await evidence(world, "Alex returned the last library book and closed the card")
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            result = await scope.update_fact({"fact_id": fact, "updates": {"state": "invalidated"}, "basis": on(proof)})
            assert result["status"] == "updated", result

        r.agent.play = play
        await r.run()

        assert world.docs[fact]["state"] == "invalidated"
        assert (await self._state(world)).undigested_since == NOW

    async def test_a_review_pass_that_only_revives_marks_nothing(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        proof = await evidence(world, "Alex weighed in at the Iron Temple gym this morning")
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            await scope.update_fact({"fact_id": fact, "updates": {"state": "current", "expiry": {"kind": "horizon", "days": 30}},
                                     "basis": on(proof)})

        r.agent.play = play
        await r.run()

        assert world.docs[fact]["state"] == "current" and (await self._state(world)).undigested_since is None

    async def test_a_timeout_that_archives_marks_the_account_and_one_that_revives_does_not(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        keep = await current(world, "Alex reads at the Example Library")
        drop = await current(world, "Alex plans a weekend in Coimbra")
        await timing_out(world, "q-keep", keep, status=QuestionStatus.ASKED, fallback="revive")
        await Rig(world).run()
        assert (await self._state(world)).undigested_since is None

        await timing_out(world, "q-drop", drop, status=QuestionStatus.ASKED, fallback="archive")
        await Rig(world).run()

        assert world.docs[drop]["state"] == "archived"
        assert (await self._state(world)).undigested_since == NOW

    @pytest.mark.parametrize("kind, marks", [
        (AnswerKind.ENDED, True), (AnswerKind.NEVER, True), (AnswerKind.STILL_TRUE, False), (AnswerKind.DONT_KNOW, False),
    ])
    async def test_an_owner_answer_marks_the_account_when_it_sets_the_fact_aside(self, kind, marks):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale_fact(world)
        await question_on(world, fact)

        await answer_service(world).apply(ACC, ANSWERER, "q1", kind, "digit", NOW)

        assert ((await self._state(world)).undigested_since == NOW) is marks

    async def test_a_dont_know_answer_on_a_question_that_archives_marks_the_account(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        fact = await stale_fact(world)
        await asked(world, "q1", fact, fallback="archive")

        await answer_service(world).apply(ACC, USER, "q1", AnswerKind.DONT_KNOW, "digit", NOW)

        assert world.docs[fact]["state"] == "archived" and (await self._state(world)).undigested_since == NOW

    async def test_a_second_close_keeps_the_first_instant(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC)
        first = await stale_fact(world, "Alex keeps a weight log")
        second = await stale_fact(world, "Alex plans a weekend in Coimbra")
        await question_on(world, first, question_id="q1")
        await question_on(world, second, question_id="q2")
        at = NOW + timedelta(hours=5)

        await answer_service(world).apply(ACC, ANSWERER, "q1", AnswerKind.NEVER, "digit", NOW)
        await answer_service(world).apply(ACC, ANSWERER, "q2", AnswerKind.ENDED, "button", at)

        assert (await self._state(world)).undigested_since == NOW

    async def test_a_digest_clears_it_and_the_next_close_sets_a_new_instant(self):
        world = await due_world()
        await closed(world, "Alex keeps a weight log")
        await Rig(world).run()
        assert (await self._state(world)).undigested_since is None
        fact = await stale_fact(world, "Alex plans a weekend in Coimbra")
        await question_on(world, fact)
        later = NOW + timedelta(days=1)

        await answer_service(world).apply(ACC, ANSWERER, "q1", AnswerKind.NEVER, "digit", later)

        assert (await self._state(world)).undigested_since == later

    async def test_an_answer_for_an_account_without_a_review_state_still_applies(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)

        await answer_service(world).apply(ACC, ANSWERER, "q1", AnswerKind.NEVER, "digit", NOW)

        assert world.docs[fact]["state"] == "invalidated" and world.state_doc(ACC) is None

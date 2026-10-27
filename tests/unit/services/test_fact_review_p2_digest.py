"""Facts P2 · card B: the weekly digest's two new blocks (P2-3, C10): facts the silence wrote as history, and the open invoice
questions that wait for the Cabinet. The plain service on the review world; the digest goes out through the world's
recording channel."""
from datetime import timedelta

from src.domain.entities import ExpiryKind
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.language import LanguageCode
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_digest import closed, digests, due_world
from tests.unit.services.test_fact_review_selection import USER, Rig

TEXTS = {"happened": "Gave a talk at the Hamburg conference", "not_happened": "Did not give the talk",
         "unknown": "Planned a talk at the Hamburg conference; unknown whether it took place"}
UNKNOWN_UK = "Планував доповідь на конференції в Гамбурзі; невідомо, чи вона відбулася"
CABINET = "https://cabinet.example/cabinet?tab=memory"
HISTORY_HEADER = "Recorded as history without your answer:"
INVOICES_HEADER = "Invoices with no payment confirmation:"
CABINET_LINE = "Mark the paid ones in the Cabinet:"


async def silent_history(world: ReviewWorld, closed_days_ago: float = 1, question_id: str = "qh",
                         closed_by: ClosedBy = ClosedBy.TIMEOUT):
    """A `happened` question whose silence closed it `closed_days_ago` days before now (its fact written as history)."""
    fact = await world.seed("Alex gives a talk at the Hamburg conference", expiry_kind=ExpiryKind.NONE)
    asked = await world.seed_question(
        QuestionStatus.ASKED, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact,
        quoted_text="Alex gives a talk at the Hamburg conference", question="happened", texts=TEXTS,
        fallback="history", asked_at=NOW - timedelta(days=20))
    return await world.put_question(asked.closed(closed_by, NOW - timedelta(days=closed_days_ago)))


async def invoice_question(world: ReviewWorld, text: str, question_id: str, queued_days_ago: float):
    fact = await world.seed(text, expiry_kind=ExpiryKind.NONE)
    return await world.seed_question(
        QuestionStatus.QUEUED, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact, quoted_text=text,
        question="paid", delivery="digest", fallback="history", texts={"paid": "paid", "not_paid": "not paid"},
        queue_key=NOW - timedelta(days=queued_days_ago), queued_at=NOW - timedelta(days=queued_days_ago))


async def test_a_digest_with_only_a_history_block_is_sent_and_marks_itself():
    world = await due_world()
    await silent_history(world, 2)

    assert await Rig(world).run() == {"status": "completed"}

    assert digests(world) == [f"{HISTORY_HEADER}\n• «{TEXTS['unknown']}»"]
    state = await world.review_store.get_state(ACC)
    assert state.last_digest_at == NOW and state.undigested_since is None


async def test_only_the_histories_since_the_last_digest_and_only_those_a_timeout_wrote_are_listed():
    world = await due_world()                                                  # the last digest was eight days ago
    await silent_history(world, 2, "q-new")
    await silent_history(world, 9, "q-old")                                    # closed before the last digest
    await silent_history(world, 1, "q-owner", ClosedBy.RESOLVED_ELSEWHERE)     # not a silence

    await Rig(world).run()

    [text] = digests(world)
    assert text.count("•") == 1 and TEXTS["unknown"] in text


async def test_a_digest_with_only_invoices_ends_with_the_cabinet_line_and_the_url():
    world = await due_world()
    await invoice_question(world, "The phone bill of October arrived", "qi1", 3)
    await invoice_question(world, "The car service bill of September arrived", "qi2", 10)

    await Rig(world).run()

    assert digests(world) == [
        "\n".join([INVOICES_HEADER, "• «The car service bill of September arrived»", "• «The phone bill of October arrived»",
                   CABINET_LINE, CABINET])]


async def test_all_three_blocks_come_in_order_separated_by_blank_lines():
    world = await due_world()
    await closed(world, "Alex keeps a weight log", "review", 1)
    await silent_history(world, 2)
    await invoice_question(world, "The phone bill of October arrived", "qi1", 3)

    await Rig(world).run()

    [text] = digests(world)
    assert text.split("\n\n") == [
        "Facts I put aside this week:\n• «Alex keeps a weight log» — no longer true",
        "To bring one back, tell me in chat that it is still true.",
        f"{HISTORY_HEADER}\n• «{TEXTS['unknown']}»",
        f"{INVOICES_HEADER}\n• «The phone bill of October arrived»\n{CABINET_LINE}\n{CABINET}",
    ]


async def test_the_lines_are_translated_when_a_translation_is_stored_and_in_english_otherwise():
    world = await due_world()
    world.language.preferences[USER] = (LanguageCode.UK, False)
    await silent_history(world, 2)
    await invoice_question(world, "The phone bill of October arrived", "qi1", 3)
    await world.review_store.set_quote_translations(
        ACC, [QuoteTranslation("uk", quote_digest(TEXTS["unknown"]), UNKNOWN_UK)], NOW)

    await Rig(world).run()

    [text] = digests(world)
    assert f"«{UNKNOWN_UK}»" in text and TEXTS["unknown"] not in text
    assert "«The phone bill of October arrived»" in text                       # no translation stored: as written


async def test_an_open_invoice_question_alone_does_not_make_a_digest_due():
    world = ReviewWorld()
    await world.notification_state.save_primary(USER, "slack", "D1")
    await world.seed_state(account_id=ACC)                                     # nothing undigested
    await invoice_question(world, "The phone bill of October arrived", "qi1", 3)

    await Rig(world).run()

    assert digests(world) == []

"""The redraws of a question message keep the fact in the review's translation: after a button answer, when a click is
refused and the options are kept, and when the review closes the question."""
from src.adapters.firestore_fact_review_store import question_from
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from src.domain.fact_review_question import ClosedBy
from src.domain.language import LanguageCode
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_question_choice import USER, TEXT, click, closer, question_on, ref_of, stale_fact

TEXT_UK = "Користувач тренується в залі Iron Temple."


async def asked_in_ukrainian(world: ReviewWorld) -> str:
    world.language.preferences[USER] = (LanguageCode.UK, False)
    fact = await stale_fact(world)
    await world.review_store.set_quote_translations(
        ACC, [QuoteTranslation("uk", quote_digest(TEXT), TEXT_UK)], world.now,
    )
    await question_on(world, fact)
    return fact


async def test_a_click_redraw_keeps_the_translated_quote():
    world = ReviewWorld()
    await asked_in_ukrainian(world)

    outcome = await click(world, "still_true")

    shown = world.interactive.shown[ref_of(world)]
    assert outcome is not None and shown.message.quote == TEXT_UK and shown.chosen_option_id == "still_true"


async def test_a_kept_redraw_keeps_the_translated_quote():
    world = ReviewWorld()
    fact = await asked_in_ukrainian(world)
    world.docs[fact]["state"] = "archived"            # closed meanwhile: the click is refused, the options are kept

    outcome = await click(world, "still_true")

    assert outcome is not None and outcome.keep_options
    assert outcome.message.quote == TEXT_UK


async def test_a_review_close_redraw_keeps_the_translated_quote():
    world = ReviewWorld()
    await asked_in_ukrainian(world)
    question = question_from(world.question_docs["q1"])

    assert await closer(world).close_question(question, ClosedBy.REVIEW) is True

    shown = world.interactive.shown[ref_of(world)]
    assert shown.message.quote == TEXT_UK and shown.note

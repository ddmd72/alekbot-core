"""The digest quotes each fact as the owner is shown it: translated when a translation holds, else as stored."""
from src.domain.entities import FactState
from src.domain.fact_review_digest import digest_message
from src.domain.fact_review_language import CANONICAL_LABELS
from src.domain.fact_review_quote_translation import QuoteTranslation, quote_digest
from tests.support.fact_kit import make_fact

LABELS = dict(CANONICAL_LABELS)
LOG = "Alex keeps a weight log"
LOG_UK = "Алекс веде  журнал ваги"


def lines(text):
    return text.split("\n")


def test_a_translated_line_has_its_translation_with_whitespace_collapsed():
    fact = make_fact(LOG, state=FactState.ARCHIVED)

    text = digest_message(LABELS, [fact], {quote_digest(LOG): QuoteTranslation("uk", quote_digest(LOG), LOG_UK)}, "uk")

    assert lines(text)[1] == "• «Алекс веде журнал ваги» — no longer true"


def test_an_untranslated_line_is_as_stored():
    translated, plain = make_fact(LOG, state=FactState.ARCHIVED), make_fact("Alex  orders\na cookbook", state=FactState.INVALIDATED)

    text = digest_message(LABELS, [translated, plain], {quote_digest(LOG): QuoteTranslation("uk", quote_digest(LOG), "журнал")}, "uk")

    assert lines(text)[2] == "• «Alex orders a cookbook» — never was true"


def test_a_record_of_another_language_is_ignored_and_no_translation_is_the_old_message():
    fact = make_fact(LOG, state=FactState.ARCHIVED)
    french = {quote_digest(LOG): QuoteTranslation("fr", quote_digest(LOG), "journal de poids")}

    assert lines(digest_message(LABELS, [fact], french, "uk"))[1] == f"• «{LOG}» — no longer true"
    assert digest_message(LABELS, [fact]) == digest_message(LABELS, [fact], {}, None)

"""Email search params (search slice 4): the mail words and the caps the other modules rely on."""
import unicodedata

from src.domain.drive_index_params import CONCEPT_KEYS_MAX, MEANING_POOL
from src.domain.email_card_labels import MAIL_DOC_TYPES
from src.domain.email_search_params import (
    KEY_PAGE, LIST_LINES_MAX, MAIL_GENERIC_WORDS, MEANING_TOP, MONTHS_PER_FETCH, SCOPE_ALL_LINES, SHOWN_FIRST,
    SHOWN_MEANING, SHOWN_REST,
)


def test_the_mail_words_are_casefolded_nfc_single_tokens():
    for word in MAIL_GENERIC_WORDS:
        assert word == unicodedata.normalize("NFC", word).casefold() and " " not in word


def test_a_mail_only_doc_types_name_is_never_a_mail_word():
    assert "message" in MAIL_DOC_TYPES and not {"message", "messages"} & MAIL_GENERIC_WORDS


def test_the_mail_nouns_and_arrival_verbs_of_every_product_language_are_listed():
    for word in ("mail", "inbox", "send", "sent", "received", "correo", "enviado", "courriel", "reçu", "листи",
                 "надіслав", "письмо", "прислал"):
        assert word in MAIL_GENERIC_WORDS


def test_the_caps_agree_with_the_drive_and_the_port():
    assert MEANING_TOP == MEANING_POOL and MONTHS_PER_FETCH == CONCEPT_KEYS_MAX
    assert (SHOWN_FIRST, SHOWN_REST, SHOWN_MEANING, SCOPE_ALL_LINES, LIST_LINES_MAX) == (10, 8, 5, 50, 50)
    assert KEY_PAGE >= 100

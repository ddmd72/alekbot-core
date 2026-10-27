"""P2-3: the 28 label keys, the option labels of each question kind, the message of each kind, and the 13 new UI strings
in four languages."""
import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import digit_answer
from src.domain.fact_review_language import (
    CANONICAL_LABELS, LABEL_KEYS, OPTION_KEYS, QUESTION_KEYS, check_labels, fact_question_message, is_review_text,
    labels_digest,
)
from src.domain.fact_review_options import OPTIONS
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk
from tests.support.review_rule_builders import question
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, TEXTS

A = AnswerKind
# The S4 label digest (`labels_digest()` on main before P2): a set cached for it is no longer current.
S4_LABELS_DIGEST = "dae215bf4b56875f"

# The P2-3 table, verbatim: key -> (en, uk, fr, es).
TABLE = {
    "memory_review_question_happened": ("Did this happen?", "Це відбулося?", "Est-ce que ça a eu lieu ?", "¿Esto ocurrió?"),
    "memory_review_question_renewed": ("Was this renewed?", "Це поновили?", "L'as-tu renouvelé ?", "¿Lo renovaste?"),
    "memory_option_happened": ("It happened", "Відбулося", "Ça a eu lieu", "Sí, ocurrió"),
    "memory_option_not_happened": ("It did not", "Не відбулося", "Pas eu lieu", "No ocurrió"),
    "memory_option_renewed": ("Renewed", "Поновлено", "Renouvelé", "Renovado"),
    "memory_option_not_renewed": ("Not renewed", "Не поновлено", "Pas renouvelé", "No renovado"),
    "memory_option_not_needed": ("No longer needed", "Вже не потрібно", "Plus nécessaire", "Ya no hace falta"),
    "memory_review_question_paid": ("Was this paid?", "Це оплачено?", "Est-ce payé ?", "¿Está pagado?"),
    "memory_option_paid": ("Paid", "Оплачено", "Payé", "Pagado"),
    "memory_option_not_paid": ("Not paid", "Не оплачено", "Pas payé", "No pagado"),
    "memory_digest_history_header": (
        "Recorded as history without your answer:", "Записано як історію без твоєї відповіді:",
        "Enregistré comme historique sans ta réponse :", "Registrado como historia sin tu respuesta:",
    ),
    "memory_digest_invoices_header": (
        "Invoices with no payment confirmation:", "Рахунки без підтвердження оплати:",
        "Factures sans confirmation de paiement :", "Facturas sin confirmación de pago:",
    ),
    "memory_digest_invoices_cabinet": (
        "Mark the paid ones in the Cabinet:", "Познач оплачені в кабінеті:",
        "Coche celles qui sont payées dans le Cabinet :", "Marca las pagadas en el Cabinet:",
    ),
}
LOCALES = {"en": en, "uk": uk, "fr": fr, "es": es}


def good(**over):
    return {**CANONICAL_LABELS, **over}


def asked(kind):
    return question(question=kind, fallback="revive" if kind == "still_true" else "history", texts=TEXTS[kind])


# ------------------------------------------------------------------------------------------------ the keys

def test_the_thirteen_ui_messages_follow_the_s4_ones_and_are_named_by_their_value():
    members = list(UIMessage)
    tail = members[members.index(UIMessage.MEMORY_DIGEST_INVALIDATED) + 1:][:13]
    assert [m.value for m in tail] == [
        "memory_review_question_happened", "memory_review_question_renewed", "memory_review_question_paid",
        "memory_option_happened", "memory_option_not_happened", "memory_option_renewed", "memory_option_not_renewed",
        "memory_option_not_needed", "memory_option_paid", "memory_option_not_paid", "memory_digest_history_header",
        "memory_digest_invoices_header", "memory_digest_invoices_cabinet",
    ]
    assert all(m.name.lower() == m.value for m in tail)


def test_the_label_set_is_twenty_eight_keys():
    assert len(LABEL_KEYS) == 28
    assert set(TABLE) <= LABEL_KEYS
    assert set(CANONICAL_LABELS) == LABEL_KEYS
    assert all(is_review_text(UIMessage(key)) for key in LABEL_KEYS)


def test_the_option_keys_are_flat_over_every_answer_in_answer_order():
    assert list(OPTION_KEYS) == list(AnswerKind)
    assert all(OPTION_KEYS[kind] == f"memory_option_{kind.value}" for kind in AnswerKind)


def test_each_kind_has_its_question_line():
    assert dict(QUESTION_KEYS) == {
        "still_true": "memory_review_question", "happened": "memory_review_question_happened",
        "renewed": "memory_review_question_renewed", "paid": "memory_review_question_paid",
    }


def test_the_canonical_labels_of_the_new_keys_are_the_english_column():
    assert {key: CANONICAL_LABELS[key] for key in TABLE} == {key: row[0] for key, row in TABLE.items()}


@pytest.mark.parametrize("code", list(LOCALES))
def test_every_new_key_in_every_locale_is_the_tables_string(code):
    column = list(LOCALES).index(code)
    strings = LOCALES[code].UI_STRINGS
    assert {key: strings[key] for key in TABLE} == {key: row[column] for key, row in TABLE.items()}


@pytest.mark.parametrize("code", list(LOCALES))
def test_every_locales_label_set_passes_the_check(code):
    assert check_labels({key: LOCALES[code].UI_STRINGS[key] for key in LABEL_KEYS}) is None


def test_the_labels_digest_changed_so_a_cached_s4_set_is_stale():
    assert labels_digest() != S4_LABELS_DIGEST


# ------------------------------------------------------------------------------------------------ check_labels per kind

@pytest.mark.parametrize("kind", list(OPTIONS))
def test_two_options_of_one_kind_with_the_same_label_are_refused(kind):
    first, second = OPTIONS[kind][0], OPTIONS[kind][1]
    labels = good(**{OPTION_KEYS[second]: f"  {CANONICAL_LABELS[OPTION_KEYS[first]].upper()} "})
    assert check_labels(labels) == f"the option labels of a {kind} question must differ"


def test_options_of_different_kinds_may_share_a_label():
    # "Renewed" and "Yes" never appear in one message.
    assert check_labels(good(**{OPTION_KEYS[A.RENEWED]: "Yes", OPTION_KEYS[A.PAID]: "Yes"})) is None


def test_dont_know_must_differ_from_every_kinds_options():
    clash = good(**{OPTION_KEYS[A.DONT_KNOW]: "Paid"})
    assert check_labels(clash) == "the option labels of a paid question must differ"


@pytest.mark.parametrize("answer", [A.HAPPENED, A.NOT_NEEDED, A.NOT_PAID])
def test_a_new_option_label_is_held_to_the_option_bounds(answer):
    key = OPTION_KEYS[answer]
    assert check_labels(good(**{key: "one two three four five"})) == f"{key}: more than 4 words"


# ------------------------------------------------------------------------------------------------ the message of each kind

MESSAGES = {
    "still_true": ("Is this still true?", [("still_true", "Yes"), ("ended", "No longer"), ("never", "Never"),
                                            ("dont_know", "Don't remember")]),
    "happened": ("Did this happen?", [("happened", "It happened"), ("not_happened", "It did not"),
                                      ("dont_know", "Don't remember")]),
    "renewed": ("Was this renewed?", [("renewed", "Renewed"), ("not_renewed", "Not renewed"),
                                      ("not_needed", "No longer needed"), ("dont_know", "Don't remember")]),
    "paid": ("Was this paid?", [("paid", "Paid"), ("not_paid", "Not paid"), ("dont_know", "Don't remember")]),
}


@pytest.mark.parametrize("kind", list(MESSAGES))
def test_the_message_of_each_kind_has_its_prompt_and_its_options(kind):
    message = fact_question_message(asked(kind), CANONICAL_LABELS)
    prompt, options = MESSAGES[kind]
    assert message.prompt == prompt
    assert [(o.id, o.label) for o in message.options] == options


@pytest.mark.parametrize("kind", list(MESSAGES))
@pytest.mark.parametrize("code", list(LOCALES))
def test_the_digits_are_the_kinds_options_in_every_language(kind, code):
    labels = {key: LOCALES[code].UI_STRINGS[key] for key in LABEL_KEYS}
    message = fact_question_message(asked(kind), labels)
    digits = "".join(str(n) for n in range(1, len(OPTIONS[kind]) + 1))
    assert [digit_answer(message, d) for d in digits] == list(OPTIONS[kind])
    assert digit_answer(message, str(len(OPTIONS[kind]) + 1)) is None
    assert message.prompt == LOCALES[code].UI_STRINGS[QUESTION_KEYS[kind]]


def test_a_digest_question_would_carry_no_dont_know():
    digest = question(question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest")
    message = fact_question_message(digest, CANONICAL_LABELS)
    assert [o.id for o in message.options] == ["paid", "not_paid"]


def test_the_message_still_needs_every_label():
    partial = dict(CANONICAL_LABELS)
    del partial["memory_option_not_paid"]
    with pytest.raises(ValueError, match="memory_option_not_paid"):
        fact_question_message(asked("still_true"), partial)

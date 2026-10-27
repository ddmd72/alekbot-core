"""The language of what the memory review says to the owner, and the labels it says it with (Facts S4 plan §3.17).

Which language: the response-language setting decides. Fixed -> that language; mirror -> the language the last review
pass reported from the owner's recent messages. The texts: UI strings for uk/en/fr/es; for any other language the review
model's translation of `CANONICAL_LABELS`, kept only if `check_labels` passes. There is no word table in code, and the
digits number the options of the question's kind (`fact_review_options.OPTIONS`), the same in every language."""
import hashlib
import json
import re
import unicodedata
from types import MappingProxyType
from typing import Final, List, Literal, Mapping, Optional, Tuple

from .fact_review_answer_kind import AnswerKind
from .fact_review_answers import option_id
from .fact_review_options import OPTIONS, options_of
from .fact_review_question import FACT_QUESTION_KIND, FactReviewQuestion, QuestionKind
from .fact_review_state import FactReviewState
from .interactive_binding import InteractiveBinding
from .interactive_message import InteractiveMessage
from .interactive_option import MAX_LABEL_CHARS, InteractiveOption
from .fact_review_quote_translation import (
    FRAME_CHARS,
    INVISIBLE_CATEGORIES,
    URL_PATTERN,
    QuoteTranslation,
    shown_quote,
    translations_wanted,
)
from .language import LanguageCode
from .ui_messages import UIMessage

MAX_TEXT_CHARS: Final[int] = 120  # the question line, the hint, the acknowledgements, the closed note, the digest lines
MAX_OPTION_WORDS: Final[int] = 4

# The label of each answer's option, in `AnswerKind` order; a question offers its kind's options (`options_of`).
OPTION_KEYS: Final[Mapping[AnswerKind, str]] = MappingProxyType({
    AnswerKind.STILL_TRUE: UIMessage.MEMORY_OPTION_STILL_TRUE.value,
    AnswerKind.ENDED: UIMessage.MEMORY_OPTION_ENDED.value,
    AnswerKind.NEVER: UIMessage.MEMORY_OPTION_NEVER.value,
    AnswerKind.DONT_KNOW: UIMessage.MEMORY_OPTION_DONT_KNOW.value,
    AnswerKind.HAPPENED: UIMessage.MEMORY_OPTION_HAPPENED.value,
    AnswerKind.NOT_HAPPENED: UIMessage.MEMORY_OPTION_NOT_HAPPENED.value,
    AnswerKind.RENEWED: UIMessage.MEMORY_OPTION_RENEWED.value,
    AnswerKind.NOT_RENEWED: UIMessage.MEMORY_OPTION_NOT_RENEWED.value,
    AnswerKind.NOT_NEEDED: UIMessage.MEMORY_OPTION_NOT_NEEDED.value,
    AnswerKind.PAID: UIMessage.MEMORY_OPTION_PAID.value,
    AnswerKind.NOT_PAID: UIMessage.MEMORY_OPTION_NOT_PAID.value,
})
_QUESTION: Final[str] = UIMessage.MEMORY_REVIEW_QUESTION.value
# The question line of each kind.
QUESTION_KEYS: Final[Mapping[QuestionKind, str]] = MappingProxyType({
    "still_true": _QUESTION,
    "happened": UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value,
    "renewed": UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value,
    "paid": UIMessage.MEMORY_REVIEW_QUESTION_PAID.value,
})
_HINT: Final[str] = UIMessage.MEMORY_REPLY_HINT.value
_TEXT_KEYS: Final[tuple[str, ...]] = (
    _QUESTION, _HINT,
    UIMessage.MEMORY_ANSWER_NOTED.value, UIMessage.MEMORY_REPLY_NOTED.value, UIMessage.MEMORY_ALREADY_ANSWERED.value,
    UIMessage.MEMORY_FACT_CHANGED.value,
    UIMessage.MEMORY_QUESTION_CLOSED.value, UIMessage.MEMORY_DIGEST_HEADER.value, UIMessage.MEMORY_DIGEST_FOOTER.value,
    UIMessage.MEMORY_DIGEST_ARCHIVED.value, UIMessage.MEMORY_DIGEST_INVALIDATED.value,
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value, UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value,
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value,
)
LABEL_KEYS: Final[frozenset[str]] = frozenset((*OPTION_KEYS.values(), *QUESTION_KEYS.values(), *_TEXT_KEYS))


def is_review_text(message: UIMessage) -> bool:
    """Whether `message` is one of the texts the memory review says (`LABEL_KEYS`)."""
    return message.value in LABEL_KEYS


# The English texts the model translates (the `en` UI strings, which a test pins equal to these).
CANONICAL_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    _QUESTION: "Is this still true?",
    OPTION_KEYS[AnswerKind.STILL_TRUE]: "Yes",
    OPTION_KEYS[AnswerKind.ENDED]: "No longer",
    OPTION_KEYS[AnswerKind.NEVER]: "Never",
    OPTION_KEYS[AnswerKind.DONT_KNOW]: "Don't remember",
    _HINT: "Reply to this message with a digit, or in your own words.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Thank you, recorded.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Noted — I'll take it into account.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Already answered, thank you.",
    UIMessage.MEMORY_FACT_CHANGED.value: "That fact has changed in the meantime, so I didn't apply your answer; I'll come back to it.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "This question is closed.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Facts I put aside this week:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "To bring one back, tell me in chat that it is still true.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "no longer true",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "never was true",
    QUESTION_KEYS["happened"]: "Did this happen?",
    QUESTION_KEYS["renewed"]: "Was this renewed?",
    QUESTION_KEYS["paid"]: "Was this paid?",
    OPTION_KEYS[AnswerKind.HAPPENED]: "It happened",
    OPTION_KEYS[AnswerKind.NOT_HAPPENED]: "It did not",
    OPTION_KEYS[AnswerKind.RENEWED]: "Renewed",
    OPTION_KEYS[AnswerKind.NOT_RENEWED]: "Not renewed",
    OPTION_KEYS[AnswerKind.NOT_NEEDED]: "No longer needed",
    OPTION_KEYS[AnswerKind.PAID]: "Paid",
    OPTION_KEYS[AnswerKind.NOT_PAID]: "Not paid",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Recorded as history without your answer:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Invoices with no payment confirmation:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Mark the paid ones in the Cabinet:",
})


def labels_digest() -> str:
    """A stable hash of `CANONICAL_LABELS` (keys and English texts): a label set written for other texts is stale."""
    canonical = json.dumps(dict(sorted(CANONICAL_LABELS.items())), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# Where the texts of a message language come from (`label_source`).
LabelSource = Literal["ui", "model", "fallback"]

_CODE = re.compile(r"[a-z]{2}")
_URL = URL_PATTERN
# `[` would imitate a history frame (`[System:`, `[Reply to memory review`). What a platform reads as control syntax in
# a text is that platform's adapter to escape, not a rule of the label.
_FORBIDDEN = FRAME_CHARS
_INVISIBLE = INVISIBLE_CATEGORIES  # control and format characters: bidi overrides, zero-width spaces


def parse_message_language(raw: object) -> Optional[str]:
    """A language code the review reported, as the one form kept: two ASCII letters, lowercased ("RU" -> "ru"); anything
    else ("ru-RU", "Russian", "", a number, None) is no language."""
    if not isinstance(raw, str):
        return None
    code = raw.strip().lower()
    return code if _CODE.fullmatch(code) else None


def ui_strings_language(code: str) -> Optional[LanguageCode]:
    """The language whose UI strings serve `code` (uk, en, fr, es), or None when the texts must be model-written."""
    parsed = parse_message_language(code)
    return next((language for language in LanguageCode if language.value == parsed), None)


def label_source(code: str, cached: Optional[Mapping[str, object]]) -> LabelSource:
    """Where the texts for `code` come from: `ui` (the UI strings), `model` (the label set the review cached for it, which
    must pass `check_labels` now, whenever it was written), else `fallback` (the UI language's texts)."""
    if ui_strings_language(code) is not None:
        return "ui"
    if parse_message_language(code) is None:
        return "fallback"
    return "model" if cached is not None and check_labels(cached) is None else "fallback"


def current_labels(state: Optional[FactReviewState], code: str) -> Optional[Mapping[str, str]]:
    """The label set the review cached for `code`, only when it was written for the current canonical texts (its digest
    is `labels_digest()`); a set written for other texts, or with no digest, is absent until a new one is written."""
    if state is None:
        return None
    cached = state.labels.get(code)
    if cached is None or state.label_digests.get(code) != labels_digest():
        return None
    return cached


def review_labels(
    code: str, state: Optional[FactReviewState], ui_language: LanguageCode,
) -> Tuple[LabelSource, Optional[LanguageCode], Optional[Mapping[str, str]]]:
    """Where the review's texts in `code` come from: `(source, strings language, cached set)`.

    The cached set is the account's current one for `code` (`current_labels`: written for today's canonical texts) and
    `label_source` decides over it. `ui`: the UI strings of `code`'s own language, the cache ignored. `model`: the cached
    set, and no strings language. `fallback`: the UI strings of `ui_language`. The strings language is set exactly when
    the cached set is not."""
    cached = current_labels(state, code)
    source = label_source(code, cached)
    if source == "model":
        return source, None, cached
    return source, ui_strings_language(code) or ui_language, None


def fixed_message_language(
    preferred: Optional[LanguageCode], agent_mirror: bool, system_default: LanguageCode,
) -> Optional[str]:
    """The fixed message language (preferred, else the system default), or None in mirror mode."""
    if agent_mirror:
        return None
    return (preferred or system_default).value


def translation_language(mirror: bool, fixed: Optional[str], reported: object) -> Optional[str]:
    """The language a pass's translations are in: mirror -> the language the report gave; fixed -> the fixed language.
    None unless `translations_wanted` for it."""
    code = parse_message_language(reported if mirror else fixed)
    return code if translations_wanted(code) else None


def shown_language(code: str, state: Optional[FactReviewState], ui_language: LanguageCode) -> str:
    """The language the review's lines in `code` are actually in: the strings language of `review_labels` when it names
    one, else `code` (model labels)."""
    _, strings_language, _ = review_labels(code, state, ui_language)
    return strings_language.value if strings_language is not None else code


def message_language(
    preferred: Optional[LanguageCode],
    agent_mirror: bool,
    system_default: LanguageCode,
    state: Optional[FactReviewState],
    ui_language: LanguageCode,
) -> str:
    """The language code (ISO 639-1) the owner's review messages are in.

    Fixed (`agent_mirror` False): the owner's language, else the system default; always one of uk/en/fr/es. Mirror: the
    language the last pass reported from his messages (`state.message_language`, read through `parse_message_language`;
    any language), and until there is one the UI language."""
    fixed = fixed_message_language(preferred, agent_mirror, system_default)
    if fixed is not None:
        return fixed
    reported = parse_message_language(state.message_language) if state is not None else None
    return reported or ui_language.value


def language_to_store(
    state: Optional[FactReviewState], reported: object, labels: object,
) -> Optional[Tuple[str, Mapping[str, str], str]]:
    """What a mirror-mode pass's report changes in the state, as `(language, its labels, their digest)`, or None when
    it changes nothing.

    `reported` is read through `parse_message_language`; no language is nothing. For a language without UI strings a
    label set that passes `check_labels` is stored, written for today's canonical texts (`labels_digest`). Otherwise
    (UI strings, or no usable set) only the language changes: its cached labels and digest are written back as they
    were, so a good earlier set is never lost to a pass that wrote none; and when the language is the one already
    stored there is nothing to write."""
    code = parse_message_language(reported)
    if code is None:
        return None
    if ui_strings_language(code) is None and isinstance(labels, Mapping) and check_labels(labels) is None:
        return code, {key: str(value).strip() for key, value in labels.items()}, labels_digest()
    if state is not None and state.message_language == code:
        return None
    cached = state.labels.get(code, {}) if state is not None else {}
    digest = state.label_digests.get(code, "") if state is not None else ""
    return code, dict(cached), digest


def check_labels(labels: object) -> Optional[str]:
    """Why a model-written label set may not be used, or None when it may.

    It must be a mapping of exactly `LABEL_KEYS`, each a text of one line with no digit (the options are numbered by the message),
    no `[`, no control or format character (a bidi override could move the digits) and no URL. Every option label is at most `MAX_OPTION_WORDS` words and `MAX_LABEL_CHARS`
    characters (the bound an option takes, so a set that passes can always be posted), and the options of one question
    kind differ from each other; every other text is at most `MAX_TEXT_CHARS` characters."""
    if not isinstance(labels, Mapping):
        return "the labels are not an object"
    if set(labels) != LABEL_KEYS:
        return "the labels are not exactly the expected keys"
    option_keys = set(OPTION_KEYS.values())
    for key in sorted(LABEL_KEYS):
        problem = _text_problem(labels[key], key in option_keys)
        if problem is not None:
            return f"{key}: {problem}"
    for kind, answers in OPTIONS.items():
        options = [str(labels[OPTION_KEYS[answer]]).strip().casefold() for answer in answers]
        if len(set(options)) != len(options):
            return f"the option labels of a {kind} question must differ"
    return None


def _text_problem(value: object, is_option: bool) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return "not a text"
    text = value.strip()
    if len(text.splitlines()) != 1:
        return "more than one line"
    if any(unicodedata.category(ch) in _INVISIBLE for ch in text):
        return "has a control or format character"
    if any(ch.isdigit() for ch in text):
        return "has a digit"
    if any(ch in text for ch in _FORBIDDEN):
        return "has ["
    if _URL.search(text):
        return "has a URL"
    if len(text) > (MAX_LABEL_CHARS if is_option else MAX_TEXT_CHARS):
        return "too long"
    if is_option and len(text.split()) > MAX_OPTION_WORDS:
        return f"more than {MAX_OPTION_WORDS} words"
    return None


def fact_question_message(
    question: FactReviewQuestion,
    labels: Mapping[str, str],
    translation: Optional[QuoteTranslation] = None,
    language: Optional[str] = None,
) -> InteractiveMessage:
    """The one message that asks the owner about a fact: the question line of its kind (`QUESTION_KEYS`), the fact as the
    owner is shown it (its translation into `language` when it has one that holds, else as stored), the options of its
    kind (`options_of`, digits from 1; each option's id is its answer) and the hint on how to reply. `labels` holds every
    key of `LABEL_KEYS`, from the UI strings or a label set that passed `check_labels`."""
    missing: List[str] = sorted(LABEL_KEYS - labels.keys())
    if missing:
        raise ValueError(f"the labels lack: {', '.join(missing)}")
    options = tuple(
        InteractiveOption(id=option_id(kind), label=labels[OPTION_KEYS[kind]]) for kind in options_of(question)
    )
    return InteractiveMessage(
        prompt=labels[QUESTION_KEYS[question.question]],
        quote=shown_quote(question.quoted_text, translation, language),
        options=options,
        binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id=question.question_id),
        reply_hint=labels[_HINT],
    )

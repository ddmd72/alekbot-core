"""The language of what the memory review says to the owner, and the labels it says it with (Facts S4 plan §3.17).

Which language: the response-language setting decides. Fixed -> that language; mirror -> the language the last review
pass reported from the owner's recent messages. The texts: UI strings for uk/en/fr/es; for any other language the review
model's translation of `CANONICAL_LABELS`, kept only if `check_labels` passes. There is no word table in code, and the
digits 1-4 mean the same in every language."""
import re
import unicodedata
from types import MappingProxyType
from typing import Final, List, Literal, Mapping, Optional

from .fact_review_answer_kind import AnswerKind
from .fact_review_question import FACT_QUESTION_KIND, FactReviewQuestion
from .fact_review_state import FactReviewState
from .interactive_binding import InteractiveBinding
from .interactive_message import InteractiveMessage
from .interactive_option import MAX_LABEL_CHARS, InteractiveOption
from .language import LanguageCode
from .ui_messages import UIMessage

MAX_TEXT_CHARS: Final[int] = 120  # the question line, the hint, the acknowledgements, the closed note, the digest lines
MAX_OPTION_WORDS: Final[int] = 4

# The label of each answer's option, in the order the options are offered (the digits 1-4).
OPTION_KEYS: Final[Mapping[AnswerKind, str]] = MappingProxyType({
    AnswerKind.STILL_TRUE: UIMessage.MEMORY_OPTION_STILL_TRUE.value,
    AnswerKind.ENDED: UIMessage.MEMORY_OPTION_ENDED.value,
    AnswerKind.NEVER: UIMessage.MEMORY_OPTION_NEVER.value,
    AnswerKind.DONT_KNOW: UIMessage.MEMORY_OPTION_DONT_KNOW.value,
})
_QUESTION: Final[str] = UIMessage.MEMORY_REVIEW_QUESTION.value
_HINT: Final[str] = UIMessage.MEMORY_REPLY_HINT.value
_TEXT_KEYS: Final[tuple[str, ...]] = (
    _QUESTION, _HINT,
    UIMessage.MEMORY_ANSWER_NOTED.value, UIMessage.MEMORY_REPLY_NOTED.value, UIMessage.MEMORY_ALREADY_ANSWERED.value,
    UIMessage.MEMORY_QUESTION_CLOSED.value, UIMessage.MEMORY_DIGEST_HEADER.value, UIMessage.MEMORY_DIGEST_FOOTER.value,
)
LABEL_KEYS: Final[frozenset[str]] = frozenset((*OPTION_KEYS.values(), *_TEXT_KEYS))

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
    UIMessage.MEMORY_QUESTION_CLOSED.value: "This question is closed.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Facts I put aside this week:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "To bring one back, tell me in chat that it is still true.",
})

# Where the texts of a message language come from (`label_source`).
LabelSource = Literal["ui", "model", "fallback"]

_CODE = re.compile(r"[a-z]{2}")
_URL = re.compile(r"(?i)[a-z][a-z0-9+.\-]*://|www\.")
# `[` would imitate a history frame (`[System:`, `[Reply to memory review`); `<` and `>` are Slack's control syntax
# (`<!channel>`, `<@U1>`), which a model-written text must not carry into a post.
_FORBIDDEN = frozenset("[<>")
_INVISIBLE = frozenset({"Cc", "Cf"})  # control and format characters: bidi overrides, zero-width spaces


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
    if not agent_mirror:
        return (preferred or system_default).value
    reported = parse_message_language(state.message_language) if state is not None else None
    return reported or ui_language.value


def check_labels(labels: Mapping[str, object]) -> Optional[str]:
    """Why a model-written label set may not be used, or None when it may.

    It must be exactly `LABEL_KEYS`, each a text of one line with no digit (the options are numbered by the message),
    no `[`, `<` or `>`, no control or format character (a bidi override could move the digits) and no URL. The four option labels are at most `MAX_OPTION_WORDS` words and `MAX_LABEL_CHARS`
    characters (the bound an option takes, so a set that passes can always be posted) and differ from each other; every
    other text is at most `MAX_TEXT_CHARS` characters."""
    if set(labels) != LABEL_KEYS:
        return "the labels are not exactly the expected keys"
    for key in sorted(LABEL_KEYS):
        problem = _text_problem(labels[key], key in OPTION_KEYS.values())
        if problem is not None:
            return f"{key}: {problem}"
    options = [str(labels[key]).strip().casefold() for key in OPTION_KEYS.values()]
    if len(set(options)) != len(options):
        return "the four option labels must differ"
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
        return "has [, < or >"
    if _URL.search(text):
        return "has a URL"
    if len(text) > (MAX_LABEL_CHARS if is_option else MAX_TEXT_CHARS):
        return "too long"
    if is_option and len(text.split()) > MAX_OPTION_WORDS:
        return f"more than {MAX_OPTION_WORDS} words"
    return None


def fact_question_message(question: FactReviewQuestion, labels: Mapping[str, str]) -> InteractiveMessage:
    """The one message that asks the owner about a fact: the question line, the fact as stored, the four options in the
    order of `AnswerKind` (digits 1-4; each option's id is its answer) and the hint on how to reply. `labels` holds every
    key of `LABEL_KEYS`, from the UI strings or a label set that passed `check_labels`."""
    missing: List[str] = sorted(LABEL_KEYS - labels.keys())
    if missing:
        raise ValueError(f"the labels lack: {', '.join(missing)}")
    options = tuple(InteractiveOption(id=kind.value, label=labels[key]) for kind, key in OPTION_KEYS.items())
    return InteractiveMessage(
        prompt=labels[_QUESTION],
        quote=question.quoted_text,
        options=options,
        binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id=question.question_id),
        reply_hint=labels[_HINT],
    )

"""The message that asks the owner about a fact's profile (fact profile RFC §4.7): the question line of its kind and the
printed words, the fact as the owner is shown it, the candidates the question carries, then the fixed options of the kind
and the usual reply hint. A digit or a button picks an option; the options' ids are the ones `profile_option_answer`
reads (`pick1`…`pick3`, `keep`, `not_listed`, `dont_know`).

Place: the atlas rows of that name (up to three), "keep as written" (a real place the atlas lacks) and "don't remember".
Person: the fitting list entries (up to three), "not on my list" and "don't remember". Date: "don't remember" only; the
date itself is a reply in words, read by code."""
from types import MappingProxyType
from typing import Final, Mapping, Optional, Tuple

from .fact_review_question import FACT_QUESTION_KIND, FactReviewQuestion
from .fact_review_quote_translation import QuoteTranslation, shown_quote
from .interactive_binding import InteractiveBinding
from .interactive_message import InteractiveMessage
from .interactive_option import InteractiveOption
from .ui_messages import UIMessage

KEEP_OPTION: Final[str] = "keep"
NOT_LISTED_OPTION: Final[str] = "not_listed"
DONT_KNOW_OPTION: Final[str] = "dont_know"

# The question line of each profile kind, and the label of each fixed option, by the key of the UI string.
PROFILE_QUESTION_KEYS: Final[Mapping[str, str]] = MappingProxyType({
    "place": UIMessage.MEMORY_PROFILE_PLACE_QUESTION.value,
    "person": UIMessage.MEMORY_PROFILE_PERSON_QUESTION.value,
    "date": UIMessage.MEMORY_PROFILE_DATE_QUESTION.value,
})
PROFILE_OPTION_KEYS: Final[Mapping[str, str]] = MappingProxyType({
    KEEP_OPTION: UIMessage.MEMORY_OPTION_KEEP_AS_WRITTEN.value,
    NOT_LISTED_OPTION: UIMessage.MEMORY_OPTION_NOT_LISTED.value,
    DONT_KNOW_OPTION: UIMessage.MEMORY_OPTION_DONT_KNOW.value,
})
# The fixed options after the candidates, by kind.
FIXED_OPTIONS: Final[Mapping[str, Tuple[str, ...]]] = MappingProxyType({
    "place": (KEEP_OPTION, DONT_KNOW_OPTION),
    "person": (NOT_LISTED_OPTION, DONT_KNOW_OPTION),
    "date": (DONT_KNOW_OPTION,),
})
_HINT: Final[str] = UIMessage.MEMORY_REPLY_HINT.value


def profile_question_message(
    question: FactReviewQuestion,
    labels: Mapping[str, str],
    translation: Optional[QuoteTranslation] = None,
    language: Optional[str] = None,
) -> InteractiveMessage:
    """The message of a profile question. `labels` holds every key of `LABEL_KEYS`. The printed words are the fact's own
    and are never translated; the quoted fact is shown in its stored translation when there is one."""
    point = question.open_point
    if point is None or not question.is_profile:
        raise ValueError("only a profile question has this message")
    fixed = FIXED_OPTIONS[question.question]
    options = (
        *(InteractiveOption(id=option.id, label=option.label) for option in point.options),
        *(InteractiveOption(id=option_id, label=labels[PROFILE_OPTION_KEYS[option_id]]) for option_id in fixed),
    )
    return InteractiveMessage(
        prompt=f"{labels[PROFILE_QUESTION_KEYS[question.question]]}: {point.printed}",
        quote=shown_quote(question.quoted_text, translation, language),
        options=options,
        binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id=question.question_id),
        reply_hint=labels[_HINT],
        min_options=1,
    )

"""FactQuestionTexts: the language and the texts of what the memory review says to the owner (Facts S4 plan §3.17).

The one place the language I/O of the memory review lives: the owner's language setting, the system default, the
account's review state and the UI language are read here, and every decision over them is a domain call in
`domain/fact_review_language.py` (`message_language`, `review_labels`, `fact_question_message`). The question, its acknowledgements and its notes all come from one source, so an
acknowledgement is in the language of its question.
"""

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from ..domain.fact_lifecycle_day import CounterField, counter_day
from ..domain.fact_review_language import (
    LABEL_KEYS,
    fact_question_message,
    is_review_text,
    message_language,
    review_labels,
)
from ..domain.fact_review_question import FactReviewQuestion
from ..domain.fact_review_state import FactReviewState
from ..domain.interactive_message import InteractiveMessage
from ..domain.language import LanguageCode
from ..domain.ui_messages import UIMessage
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
from ..ports.language_service_port import LanguageServicePort
from ..ports.localization_port import LocalizationPort
from ..utils.logger import logger


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactQuestionTexts:
    """`language_for`, `labels_for`, `message_for` and `text`, each over one read of the account's review state;
    `mirrors`, over the language setting only.

    The texts of a language come from the UI strings (uk, en, fr, es), from the label set the review cached for it, or,
    when that set is missing, stale or refused, from the owner's UI language (`review_labels` decides).

    One call is one message the owner sees, counted once: each call of `labels_for`, `message_for` or `text` that falls
    back adds one `labels_fallback` to the account's day, so an event that shows several texts calls `labels_for` once
    and takes them from its result. The count is best effort: a counter that fails is a WARNING, never a lost message.
    A read that raises (the language setting, the review state, the UI language) is logged, naming the read, and
    raised: the caller decides.
    """

    def __init__(
        self,
        language: LanguageServicePort,
        localization: LocalizationPort,
        review_store: FactReviewStore,
        counters: FactLifecycleCountersPort,
        system_default: LanguageCode,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._language = language
        self._localization = localization
        self._review_store = review_store
        self._counters = counters
        self._system_default = system_default
        self._clock = clock

    async def language_for(self, user_id: str, account_id: str) -> str:
        """The ISO 639-1 code the owner's review messages are in (`message_language`)."""
        code, _, _ = await self._read(user_id, account_id)
        return code

    async def mirrors(self, user_id: str) -> bool:
        """Whether the owner's response language mirrors his messages: the review pass then reports their language
        (and, for a language without UI strings, the labels)."""
        try:
            _, agent_mirror = await self._language.get_preference(user_id)
        except Exception as exc:
            logger.error("[FactQuestionTexts] the language setting of user %s not read: %s: %s",
                         user_id[:8], type(exc).__name__, exc)
            raise
        return agent_mirror

    async def labels_for(self, user_id: str, account_id: str) -> Mapping[str, str]:
        """Every text of `LABEL_KEYS` in the message language, from the source `review_labels` names."""
        code, state, ui_language = await self._read(user_id, account_id)
        source, language, cached = review_labels(code, state, ui_language)
        if cached is not None:
            return MappingProxyType(dict(cached))
        if source == "fallback":
            logger.info(
                "[FactQuestionTexts] no usable labels for %r on account %s; using the UI language %s",
                code, account_id[:8], language.value,
            )
            await self._count_fallback(account_id)
        return MappingProxyType({key: self._localization.get_ui_string(language, UIMessage(key)) for key in LABEL_KEYS})

    async def ui_labels(self, user_id: str) -> Mapping[str, str]:
        """Every text of `LABEL_KEYS` in the user's UI language: what the Cabinet shows, which is not a message of the
        review (no message language, no cached labels, nothing counted)."""
        language = await self._language.resolve_ui_language(user_id)
        return MappingProxyType({key: self._localization.get_ui_string(language, UIMessage(key)) for key in LABEL_KEYS})

    async def message_for(self, question: FactReviewQuestion, user_id: str, account_id: str) -> InteractiveMessage:
        """The message that asks the owner about the question's fact, in the message language."""
        return fact_question_message(question, await self.labels_for(user_id, account_id))

    async def text(self, user_id: str, account_id: str, message: UIMessage) -> str:
        """One text of the review (an acknowledgement, the closed note, a digest line) from the same source as
        `labels_for`, so it is in the language of the question it answers. One call is one message the owner sees,
        counted once: a fallback here adds one `labels_fallback`, so a message built from several texts takes them from
        one `labels_for` instead. Raises ValueError for a message that is not one of the review's texts."""
        if not is_review_text(message):
            raise ValueError(f"{message.value} is not a memory review text")
        return (await self.labels_for(user_id, account_id))[message.value]

    async def _read(self, user_id: str, account_id: str) -> Tuple[str, Optional[FactReviewState], LanguageCode]:
        step = "the language setting"
        try:
            preferred, agent_mirror = await self._language.get_preference(user_id)
            step = "the review state"
            state = await self._review_store.get_state(account_id)
            step = "the UI language"
            ui_language = await self._language.resolve_ui_language(user_id)
        except Exception as exc:
            logger.error(
                "[FactQuestionTexts] %s of account %s not read: %s: %s", step, account_id[:8], type(exc).__name__, exc,
            )
            raise
        return message_language(preferred, agent_mirror, self._system_default, state, ui_language), state, ui_language

    async def _count_fallback(self, account_id: str) -> None:
        try:
            await self._counters.add(account_id, counter_day(self._clock()), {CounterField.LABELS_FALLBACK: 1})
        except Exception as exc:
            logger.warning(
                "[FactQuestionTexts] labels_fallback not counted for account %s: %s", account_id[:8], type(exc).__name__,
            )

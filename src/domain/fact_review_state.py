"""One account's memory-review bookkeeping: when it last ran, what it has sent today, which question is open."""
import re
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

# ISO 639-1 as the review reports it (two lowercase letters); the shape only, which languages work is not decided here.
_LANGUAGE = re.compile(r"[a-z]{2}")


class FactReviewState(BaseModel):
    """`current_question_id` is the one question the owner may answer by reply (none when no question is out). `sent` counts
    the questions sent on the local day `sent_on`; `delivery_failed_on` is the local day a post failed (no retry that day).
    `offers` counts how often a stale fact went to the model without a decision. `message_language` and `labels` are
    the last language the review reported for the owner's messages and the texts the model wrote for it (by language);
    `label_digests` holds, by language, the digest of the canonical texts each label set was written for.
    Frozen, and its three mappings are read-only; the store writes changes. Every datetime is timezone-aware. A deep copy
    (`copy.deepcopy`, `model_copy(deep=True)`) is rebuilt from the dump, because read-only mappings cannot be copied
    as they are."""

    model_config = ConfigDict(frozen=True)

    account_id: str = Field(min_length=1)
    owner_user_id: Optional[str] = None
    enqueued_at: Optional[AwareDatetime] = None
    started_at: Optional[AwareDatetime] = None
    last_review_at: Optional[AwareDatetime] = None
    last_failed_at: Optional[AwareDatetime] = None
    unsuccessful_since: Optional[AwareDatetime] = None
    offers: Mapping[str, int] = Field(default_factory=dict)
    last_digest_at: Optional[AwareDatetime] = None
    undigested_since: Optional[AwareDatetime] = None
    oldest_alert_at: Optional[AwareDatetime] = None
    queue_alert_at: Optional[AwareDatetime] = None
    current_question_id: Optional[str] = None
    sent_on: Optional[date] = None
    sent: int = Field(default=0, ge=0)
    delivery_failed_on: Optional[date] = None
    message_language: Optional[str] = None
    labels: Mapping[str, Mapping[str, str]] = Field(default_factory=dict)
    label_digests: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("message_language")
    @classmethod
    def _language_is_two_letters(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _LANGUAGE.fullmatch(value):
            raise ValueError("message_language is an ISO 639-1 code: two lowercase letters")
        return value

    @field_validator("offers")
    @classmethod
    def _offers_are_counts(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("offers are counts, not below zero")
        return MappingProxyType(dict(value))

    @field_validator("labels")
    @classmethod
    def _labels_are_by_language(cls, value: Mapping[str, Mapping[str, str]]) -> Mapping[str, Mapping[str, str]]:
        if any(not _LANGUAGE.fullmatch(language) for language in value):
            raise ValueError("labels are kept by ISO 639-1 code: two lowercase letters")
        return MappingProxyType({language: MappingProxyType(dict(texts)) for language, texts in value.items()})

    @field_validator("label_digests")
    @classmethod
    def _digests_are_by_language(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if any(not _LANGUAGE.fullmatch(language) for language in value):
            raise ValueError("label digests are kept by ISO 639-1 code: two lowercase letters")
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _sent_belongs_to_a_day(self) -> Self:
        if self.sent > 0 and self.sent_on is None:
            raise ValueError("sent questions are counted for a day: sent_on")
        return self

    def __deepcopy__(self, memo: Optional[Dict[int, Any]] = None) -> Self:
        return type(self).model_validate(self.model_dump())

    # ------------------------------------------------------------------------------------------------ moves
    # Each move returns the fields it changes (empty: nothing to write); the store validates the state they make and
    # writes exactly those fields.

    def sent_today(self, today: date) -> int:
        """Questions sent on the owner's local day `today`; the count of an earlier day is not today's."""
        return self.sent if self.sent_on == today else 0

    def claimed(self, question_id: str, today: date) -> Dict[str, Any]:
        """A question goes out: it becomes the current one and counts for `today` (a new day starts at 1)."""
        return {"current_question_id": question_id, "sent": self.sent_today(today) + 1, "sent_on": today}

    def released(self, question_id: str) -> Dict[str, Any]:
        """The pointer cleared when, and only when, it names this question."""
        return {"current_question_id": None} if self.current_question_id == question_id else {}

    def unrecorded_delivery_failure(self, question_id: str) -> Optional[str]:
        """Why a failed delivery of this question has no day to record, or None when it has one. `sent_on` is the day of
        the latest claim: this question's day only while the pointer names it."""
        if self.current_question_id != question_id:
            return "which is not the current question"
        if self.sent_on is None:
            return "with no claim day"
        return None

    def delivery_failed(self, question_id: str) -> Dict[str, Any]:
        """The post of this question failed: that day gets no more sends (the claim's day), or nothing to record."""
        if self.unrecorded_delivery_failure(question_id) is not None:
            return {}
        return {"delivery_failed_on": self.sent_on}

    def enqueued(self, at: datetime) -> Dict[str, Any]:
        """A review was enqueued; the alert clock starts at the first enqueue since a success."""
        return {"enqueued_at": at, "unsuccessful_since": self.unsuccessful_since or at}

    def succeeded(self, at: datetime) -> Dict[str, Any]:
        return {"last_review_at": at, "unsuccessful_since": None}

    def failed(self, at: datetime) -> Dict[str, Any]:
        """The first failure since a success starts the alert clock; a later one keeps it."""
        return {"last_failed_at": at, "unsuccessful_since": self.unsuccessful_since or at}

    def undigested(self, at: datetime) -> Dict[str, Any]:
        """A fact was set aside after the last digest; an earlier instant is kept."""
        return {"undigested_since": self.undigested_since or at}

    def digested(self, at: datetime) -> Dict[str, Any]:
        return {"last_digest_at": at, "undigested_since": None}

    def without_offers(self, fact_ids: Iterable[str]) -> Dict[str, Any]:
        """The offers of these facts dropped; the others kept."""
        removed = set(fact_ids)
        return {"offers": {fact_id: count for fact_id, count in self.offers.items() if fact_id not in removed}}

    def with_language(self, lang: str, labels: Mapping[str, str], labels_digest: str) -> Dict[str, Any]:
        """`lang` is the message language and its labels (written for the canonical texts of `labels_digest`) are
        replaced; other languages' labels and digests are kept."""
        return {
            "message_language": lang,
            "labels": {**self.labels, lang: dict(labels)},
            "label_digests": {**self.label_digests, lang: labels_digest},
        }

    @field_serializer("offers")
    def _offers_as_dict(self, value: Mapping[str, int]) -> Dict[str, int]:
        return dict(value)

    @field_serializer("label_digests")
    def _digests_as_dict(self, value: Mapping[str, str]) -> Dict[str, str]:
        return dict(value)

    @field_serializer("labels")
    def _labels_as_dicts(self, value: Mapping[str, Mapping[str, str]]) -> Dict[str, Dict[str, str]]:
        return {language: dict(texts) for language, texts in value.items()}

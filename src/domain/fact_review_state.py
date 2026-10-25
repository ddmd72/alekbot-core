"""One account's memory-review bookkeeping: when it last ran, what it has sent today, which question is open."""
import re
from datetime import date
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Self

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

    @field_serializer("offers")
    def _offers_as_dict(self, value: Mapping[str, int]) -> Dict[str, int]:
        return dict(value)

    @field_serializer("label_digests")
    def _digests_as_dict(self, value: Mapping[str, str]) -> Dict[str, str]:
        return dict(value)

    @field_serializer("labels")
    def _labels_as_dicts(self, value: Mapping[str, Mapping[str, str]]) -> Dict[str, Dict[str, str]]:
        return {language: dict(texts) for language, texts in value.items()}

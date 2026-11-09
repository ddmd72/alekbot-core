"""The profiler's answer for one fact (fact profile RFC §4.1, taxonomy §5.1): what the model proposes, validated against
the registry's enums. Code checks every proposed value afterwards (`fact_profile_derive.py`) and computes the domains;
the validated answer is stored as `profile_answer`, so a code change re-derives without a call.

`ProfileAnswer` and its three parts (`AnswerTimePoint`, `AnswerPlace`, `AnswerPerson`) are one schema, so they share
this module on purpose.
"""
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .fact_domains import subject_enum, use_area_enum

# The seven kinds (taxonomy §4): cross-domain questions, usually empty.
PROFILE_KINDS = ("preference", "constraint", "ownership", "document", "incident", "identifier", "intention")
TimeRole = Literal["event", "state", "validity", "measurement", "timeless"]
Precision = Literal["day", "month", "year"]
Sensitivity = Literal["ordinary", "discreet", "intimate"]
TaxOrFine = Literal["none", "tax", "fine"]
AboutPerson = Literal["none", "relative", "other"]


class AnswerTimePoint(BaseModel):
    """A date or span as the model read it, with the printed text it was read from."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    precision: Precision
    source: str


class AnswerPlace(BaseModel):
    """A city and country the fact is located in, with the printed text that names it."""
    model_config = ConfigDict(extra="forbid")
    city: str
    country: str
    source: str


class AnswerPerson(BaseModel):
    """A person the fact names: the name as printed, the role word, the printed text."""
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str
    source: str


class ProfileAnswer(BaseModel):
    """Every field the model answers (the profiler's JSON schema); unknown fields and out-of-enum values fail."""
    model_config = ConfigDict(extra="forbid")
    label_words: List[str]
    time_role: TimeRole
    time_points: List[AnswerTimePoint]
    places: List[AnswerPlace]
    people: List[AnswerPerson]
    tax_or_fine: TaxOrFine
    user_identity: bool
    pet: bool
    state_or_law: bool
    about_person: AboutPerson
    subject: str
    money_flow: bool
    use_areas: List[str]
    kinds: List[str]
    sensitivity: Sensitivity

    @field_validator("subject")
    @classmethod
    def _subject_in_enum(cls, value: str) -> str:
        if value not in subject_enum():
            raise ValueError(f"subject {value!r} is not in the subject enum")
        return value

    @field_validator("use_areas")
    @classmethod
    def _use_areas_in_enum(cls, value: List[str]) -> List[str]:
        unknown = [v for v in value if v not in use_area_enum()]
        if unknown:
            raise ValueError(f"use areas {unknown} are not in the use-area enum")
        return value

    @field_validator("kinds")
    @classmethod
    def _kinds_in_set(cls, value: List[str]) -> List[str]:
        unknown = [v for v in value if v not in PROFILE_KINDS]
        if unknown:
            raise ValueError(f"kinds {unknown} are not profile kinds")
        return value

    def stored(self) -> dict[str, object]:
        """The answer as `profile_answer` stores it: plain JSON values, time points keyed `from`."""
        return self.model_dump(by_alias=True)

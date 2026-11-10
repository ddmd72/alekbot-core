"""An open point of a fact profile, as a profile question to the owner carries it (fact profile RFC §4.7).

An open point is something the profile could not decide and the owner is asked: a place the atlas cannot place
(`places`), a person mention that fits a confirmed list entry (`people`), or a date the profiler flagged
(`time_points`). The record holds what the message needs and what the answer needs, read once when the question is
built: the key the answer is stored under, the words as the fact prints them, the atlas reason and the candidate options
shown. The candidates are options only: an answer by code is built from the option the owner picked (`pick1`…), which
carries the geonameid of a place or the name of a list entry.

`OpenPointOption` and `ProfileOpenPoint` are one stored shape, so they share this module on purpose."""
from typing import Final, Literal, Self, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .interactive_option import MAX_LABEL_CHARS

OpenPointField = Literal["places", "people", "time_points"]
MAX_PICKS: Final[int] = 3


class OpenPointOption(BaseModel):
    """One candidate the owner may pick: `id` is `pick1`…`pick3` in order, `label` what the owner reads, `value` the geonameid of
    the atlas row (a place) or the name of the list entry (a person)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    value: str = Field(min_length=1)


class ProfileOpenPoint(BaseModel):
    """`key` is the answer's key (`place_point_key`, `person_point_key` or `date_point_key`); `printed` the words as the
    fact prints them. A place has its `city` and `country` as the profile names them and the atlas `reason` it is open;
    a person or a date has the `lineage_id` of the fact and the flagged `mention`. A date has no options."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    field: OpenPointField
    printed: str = Field(min_length=1)
    city: str = ""
    country: str = ""
    reason: str = ""
    lineage_id: str = ""
    mention: str = ""
    options: Tuple[OpenPointOption, ...] = ()

    @model_validator(mode="after")
    def _the_point_has_what_its_field_needs(self) -> Self:
        if self.field == "places":
            if not self.city.strip():
                raise ValueError("a place point has its city")
        elif not self.lineage_id or not self.mention.strip():
            raise ValueError("a person or date point has its lineage_id and mention")
        if self.field == "time_points" and self.options:
            raise ValueError("a date point has no options")
        if len(self.options) > MAX_PICKS:
            raise ValueError(f"at most {MAX_PICKS} candidate options")
        if [option.id for option in self.options] != [f"pick{n}" for n in range(1, len(self.options) + 1)]:
            raise ValueError("the candidate options are pick1, pick2, pick3 in order")
        return self

"""A fact's retrieval profile (fact profile RFC §2.1): the fields code derives from the profiler's checked answer, and the
set of every field the profile step may write (`PROFILE_FIELDS`).

`FactProfile` and its parts (`ProfileTimePoint`, `ProfilePerson`, `UnresolvedPlace`, `ProfileReviewItem`) are one stored shape, so they
share this module on purpose.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .exceptions import InPlaceFieldRefused

# Every field the profile step writes (RFC §2.1); flat, optional fields of FactEntity. Disjoint from the in-place
# lifecycle fields and from everything a fact's writers own (a test pins both).
PROFILE_FIELDS: frozenset[str] = frozenset({
    "label_words", "label_vector", "time_role", "time_points", "place_keys", "places_unresolved", "people",
    "primary_domain", "domain_keys", "user_identity", "kinds", "sensitivity", "profile_answer", "profile_fingerprint",
    "profile_derivation", "profile_drops", "profile_model", "profiled_at", "profile_failures",
    "profile_failed_fingerprint", "profile_review_needed", "profile_review_items",
})


def require_profile_fields(fields: Mapping[str, Any]) -> None:
    """A profile write names only PROFILE_FIELDS: raises InPlaceFieldRefused (a programming error) for any other.
    Every store checks it before any I/O."""
    refused = set(fields) - PROFILE_FIELDS
    if refused:
        raise InPlaceFieldRefused(refused)


@dataclass(frozen=True)
class ProfileTimePoint:
    """A checked date or span: `start`/`end` as ISO text of `precision` ("" end = a single point), the printed source."""
    start: str
    end: str
    precision: str
    source: str

    def stored(self) -> Dict[str, str]:
        return {"from": self.start, "to": self.end, "precision": self.precision, "source": self.source}


@dataclass(frozen=True)
class ProfilePerson:
    """A printed person: name and role as the model gave them, and the keys code made of each."""
    name: str
    role: str
    name_keys: Tuple[str, ...]
    role_keys: Tuple[str, ...]
    # The name comes from the owner's confirmed list, not from the fact's text (the fact printed only a relation).
    from_list: bool = False

    def stored(self) -> Dict[str, Any]:
        stored: Dict[str, Any] = {"name": self.name, "role": self.role, "name_keys": list(self.name_keys),
                                  "role_keys": list(self.role_keys)}
        if self.from_list:
            stored["from_list"] = True   # written only when true: a person from the text keeps its stored shape
        return stored


@dataclass(frozen=True)
class UnresolvedPlace:
    """A printed place the atlas did not resolve to one row (kept for reports only)."""
    city: str
    country: str

    def stored(self) -> Dict[str, str]:
        return {"city": self.city, "country": self.country}


@dataclass(frozen=True)
class ProfileReviewItem:
    """An open point the profiler left open: the field it belongs to and the printed words it is about."""
    field: str
    mention: str

    def stored(self) -> Dict[str, str]:
        return {"field": self.field, "mention": self.mention}


@dataclass(frozen=True)
class FactProfile:
    """The derived profile of one fact; `stored()` gives the PROFILE_FIELDS the derivation owns. The fingerprint,
    derivation version, label vector, model, time and failure fields are added by the step that writes it."""
    primary_domain: str
    domain_keys: Tuple[str, ...]
    user_identity: bool
    kinds: Tuple[str, ...]
    sensitivity: str
    label_words: Tuple[str, ...] = ()
    time_role: Optional[str] = None
    time_points: Tuple[ProfileTimePoint, ...] = ()
    place_keys: Tuple[str, ...] = ()
    places_unresolved: Tuple[UnresolvedPlace, ...] = ()
    people: Tuple[ProfilePerson, ...] = ()
    profile_answer: Optional[Mapping[str, Any]] = None
    profile_drops: Mapping[str, int] = field(default_factory=dict)
    review_needed: bool = False
    review_items: Tuple[ProfileReviewItem, ...] = ()

    def stored(self) -> Dict[str, Any]:
        return {
            "primary_domain": self.primary_domain,
            "domain_keys": list(self.domain_keys),
            "user_identity": self.user_identity,
            "kinds": list(self.kinds),
            "sensitivity": self.sensitivity,
            "label_words": list(self.label_words),
            "time_role": self.time_role,
            "time_points": [p.stored() for p in self.time_points],
            "place_keys": list(self.place_keys),
            "places_unresolved": [p.stored() for p in self.places_unresolved],
            "people": [p.stored() for p in self.people],
            "profile_answer": dict(self.profile_answer) if self.profile_answer is not None else None,
            "profile_drops": dict(self.profile_drops),
            "profile_review_needed": self.review_needed,
            "profile_review_items": [i.stored() for i in self.review_items],
        }

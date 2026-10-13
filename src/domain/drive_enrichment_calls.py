"""
Drive enrichment calls — the requests and replies of Search v2's model ports
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.1-§25.4.4, frozen in §25.13.1). Pure data.

`DocumentProfilerPort.profile(ProfileRequest) -> ProfileReply`, `PassageHintPort.hints(HintRequest) -> HintReply`, and
the triage v2 reply `TriageReply` (the `ImageTriagePort` itself is unchanged until P3). Replies carry the model that
served and the cost, already billed, so the tick can count them per `UsageClass` (§25.4.5).

Public interface:
    ModelReplyStatus
    ProfileInput, ProfileLabels, ProfileRequest, ProfileReply
    HintRequest, HintReply
    TriageResultV2, TriageReply
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

from .drive_enrichment import DriveProfile, HintItem, PhotoDescription
from .drive_index import TriageVerdict


class ModelReplyStatus(str, Enum):
    """How one enrichment call ended (§25.13.1, §25.4.6)."""
    OK = "ok"
    REFUSED = "refused"                 # 400 / 413 / 422 by the primary
    FAILED = "failed"                   # charged: a 5xx, a timeout, an unexpected error, invalid output after a retry
    STOPPED_AT_CAP = "stopped_at_cap"   # still stopped at the output cap after halving
    UNAVAILABLE = "unavailable"         # an uncharged outage of this agent type


@dataclass(frozen=True)
class ProfileInput:
    """The profiler's text input, assembled from stored text by `profile_input(record, chunks, kind)` per
    `PROFILE_POLICY` (§25.3): the head (with any metadata lines, sheet rows or the README excerpt's head), the tail,
    the heading lines, and a collapsed folder's child names. The file name travels on the request, outside the
    fingerprint (§25.2.1)."""
    head: str
    tail: str = ""
    headings: Tuple[str, ...] = ()
    child_names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileLabels:
    """The previous profile's labels, passed on a re-profile as "keep unless the text contradicts" (§25.4.1); outside
    the fingerprint."""
    doc_type: str
    life_domains: Tuple[str, ...] = ()
    valid_until: Optional[date] = None


@dataclass(frozen=True)
class ProfileRequest:
    user_id: str
    account_id: str
    item_id: str
    kind: str
    name: str
    input: ProfileInput
    previous: Optional[ProfileLabels] = None


@dataclass(frozen=True)
class ProfileReply:
    """`profile` is set only with status `ok`; it is the model's output before `drive_profile.verify` (§25.4.1)."""
    status: ModelReplyStatus
    profile: Optional[DriveProfile]
    model: str
    usd: float
    fallback_used: bool


@dataclass(frozen=True)
class HintRequest:
    """`PASSAGE_CHUNKS_PER_CALL` chunks as `(index, text)` under opaque indexes, plus the profile's title and
    `what_it_is` (§25.4.2)."""
    user_id: str
    account_id: str
    item_id: str
    chunks: Sequence[Tuple[int, str]]
    title: str
    what_it_is: str


@dataclass(frozen=True)
class HintReply:
    """Items per chunk index, before `drive_hints.ground` (§25.4.2)."""
    status: ModelReplyStatus
    items_by_index: Mapping[int, Sequence[HintItem]]
    model: str
    usd: float
    fallback_used: bool


@dataclass(frozen=True)
class TriageResultV2:
    """One image's v2 triage (§25.4.3): a `photo` verdict carries a `PhotoDescription`, a `text` verdict none."""
    item_id: str
    verdict: TriageVerdict
    photo: Optional[PhotoDescription] = None


@dataclass(frozen=True)
class TriageReply:
    """The triage v2 reply (§25.13.1): one result per image answered, and what the batch cost (the v1
    `TriageBatchOutcome` plus the served model and whether the fallback served)."""
    results: Tuple[TriageResultV2, ...]
    model: str
    usd: float
    fallback_used: bool
    model_calls: int = 0         # a refused or capped batch is split and asked again (§25.4.4)

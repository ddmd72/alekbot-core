"""
Drive enrichment calls — the requests and replies of Search v2's model ports
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.1-§25.4.4, §25.4.6, frozen in §25.13.1). Pure data.

`DocumentProfilerPort.profile(ProfileRequest) -> ProfileReply`, `PassageHintPort.hints(HintRequest) -> HintReply`, and
the triage v2 reply `TriageReply` (the `ImageTriagePort` switches to it in P3). All three report every outcome as a
`ModelReplyStatus`, never as an exception, and carry the model that served and the cost already billed, with the
fallback's share in dollars, so the tick counts them per `UsageClass` and for the daily fallback alert (§25.4.5).

Several public classes in one module, by decision: the frozen core groups the port messages in one file, as
`drive_extraction` does (CLAUDE.md's one-class rule is waived here on purpose).

Public interface:
    ModelReplyStatus
    ProfileInput, ProfileLabels, ProfileRequest, ProfileReply
    HintRequest, HintReply
    TriageResultV2, TriageReply
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

from .drive_enrichment import DriveProfile, HintItem, PhotoDescription
from .drive_index import TriageVerdict


class ModelReplyStatus(str, Enum):
    """How one enrichment call (or one chunk / image of it) ended, mapped to §25.4.6's effects:

    | status           | §25.4.6 outcome                                                    | charged | backoff |
    | ok               | answered                                                           | —       | no      |
    | refused          | a refusal (400, 413, 422) by the primary                           | no      | no      |
    | failed           | a 5xx, a timeout or an unexpected error after any failover; a      | yes     | yes     |
    |                  | refusal by the fallback (LLMFallbackClientError)                   |         |         |
    | invalid          | invalid output after the parse retry                               | yes     | no      |
    | stopped_at_cap   | still stopped at the output cap after halving                      | yes     | no      |
    | unavailable      | provider unavailable (429, network, breaker, account 4xx, missing  | no      | yes     |
    |                  | key or prompt, both providers down): an outage of this agent type  |         |         |
    """
    OK = "ok"
    REFUSED = "refused"
    FAILED = "failed"
    INVALID = "invalid"
    STOPPED_AT_CAP = "stopped_at_cap"
    UNAVAILABLE = "unavailable"


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
    """`profile` is set only with status `ok`; it is the model's output before `drive_profile.verify` (§25.4.1).
    `usd` is the whole cost, `fallback_usd` the part the fallback served."""
    status: ModelReplyStatus
    profile: Optional[DriveProfile]
    model: str
    usd: float
    fallback_usd: float = 0.0


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
    """One passage call, after the runner's halving (§25.4.4). `outcome_by_index` has an entry for every chunk the
    calls settled: `ok`, `refused` (no hints, counted, never holds the file), `stopped_at_cap` (no hints, counted),
    `invalid` or `failed`. `items_by_index` holds the ungrounded items of the `ok` chunks only, and they are kept
    whatever `status` says. A chunk with no outcome was not attempted (an outage cut the batch) and is retried.
    `status` is the batch's deciding effect: `ok` when every chunk has an outcome and none is charged, else the one
    that decides the attempt and the backoff (`unavailable` before `failed` before `invalid` / `stopped_at_cap`);
    `refused` chunks alone leave it `ok`."""
    status: ModelReplyStatus
    items_by_index: Mapping[int, Sequence[HintItem]]
    model: str
    usd: float
    fallback_usd: float = 0.0
    outcome_by_index: Mapping[int, ModelReplyStatus] = field(default_factory=dict)


@dataclass(frozen=True)
class TriageResultV2:
    """One image's v2 triage (§25.4.3): a `photo` verdict carries a `PhotoDescription`, a `text` verdict none."""
    item_id: str
    verdict: TriageVerdict
    photo: Optional[PhotoDescription] = None


@dataclass(frozen=True)
class TriageReply:
    """The triage v2 reply (§25.13.1): one result per image answered. `capped_item_ids` were still stopped at the cap
    alone and `refused_item_ids` refused alone: they get no description (counted), never a photo result without one.
    An image in none of the three was not settled and is retried. `status` follows `HintReply.status`. An outage is a
    status here, not `DriveModelUnavailable`: one error contract for the three model ports."""
    status: ModelReplyStatus
    results: Tuple[TriageResultV2, ...]
    model: str
    usd: float
    fallback_usd: float = 0.0
    model_calls: int = 0         # a refused or capped batch is split and asked again (§25.4.4)
    capped_item_ids: Tuple[str, ...] = ()
    refused_item_ids: Tuple[str, ...] = ()

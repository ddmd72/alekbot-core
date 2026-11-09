"""What one profile step found and did (fact profile RFC §4.4): the scan and the run that follows it.

`FactProfileScan` and `FactProfileRun` are two frozen result dataclasses of one step, so they share this module on
purpose. A run carries its own scan.
"""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class FactProfileScan:
    """The account's current facts by profile status; `to_call` is the model calls the step would make with no cap."""
    scanned: int = 0
    missing: int = 0
    stale_input: int = 0
    stale_derivation: int = 0
    abandoned: int = 0
    fresh: int = 0
    to_call: int = 0


@dataclass(frozen=True)
class FactProfileRun:
    """One run of the step.

    rederived: profiles rebuilt from a stored answer; directives: directive profiles built without a call; called:
    facts a model call was made for; written: profiles stored (re-derivations and directives included); failed: failures
    stored; newly_abandoned: failures that reached the abandon limit; deferred: planned work not done this cycle (a call
    not started, a write past the deadline); lost_cas: writes refused because the fact changed; usd: spend of the calls;
    drops: dropped values per reason; outage: the model was unavailable; refreshed: the baseline was refreshed;
    error: the run stopped on an error (the counts are partial).
    """
    scan: FactProfileScan = field(default_factory=FactProfileScan)
    rederived: int = 0
    directives: int = 0
    called: int = 0
    written: int = 0
    failed: int = 0
    newly_abandoned: int = 0
    deferred: int = 0
    lost_cas: int = 0
    usd: float = 0.0
    drops: Mapping[str, int] = field(default_factory=dict)
    outage: bool = False
    refreshed: bool = False
    error: bool = False

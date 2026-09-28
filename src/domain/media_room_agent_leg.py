from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class AgentLeg:
    """Lelik's side: the transport adapters feeding the relay, and the offer (if any) the caller
    must answer to receive Lelik's track."""
    adapter_ids: List[str] = field(default_factory=list)
    offer_sdp: Optional[str] = None

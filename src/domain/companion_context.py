"""
CompanionContext — assembled read-side context for one companion turn.

Produced by CompanionContextAssemblerService (RFC: COMPANION_AGENTS_RFC.md
§6). Ephemeral, not persisted — a dataclass, like domain/search.py's
EnrichedContext, not a pydantic entity like CompanionRecord.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .companion import CompanionRecord
from .entities import FactEntity
from .search import EnrichedFact


@dataclass
class CompanionContext:
    session_summary: Optional[str] = None
    own_records: List[CompanionRecord] = field(default_factory=list)
    biographical_facts: List[EnrichedFact] = field(default_factory=list)
    standing_directives: List[FactEntity] = field(default_factory=list)

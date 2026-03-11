"""
AgentProfile — resolved agent profile returned by AgentProfileRepository.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from dataclasses import dataclass, field
from typing import Dict

from src.domain.prompt_v3.profile_slot import ProfileToken


@dataclass(frozen=True)
class AgentProfile:
    """Resolved agent profile: which blueprint to use + which tokens to include.

    Stored in Firestore as a single document. Document ID = agent_id (e.g. "quick").
    The blueprint_id field is the source of truth for which blueprint the agent uses.

    Fields:
        blueprint_id: ID of the blueprint to assemble against (e.g. "universal_agent_v1")
        tokens: map of token_id -> ProfileToken (order + non_overridable)

    Examples:
        >>> profile = AgentProfile(
        ...     blueprint_id="universal_agent_v1",
        ...     tokens={
        ...         "COGNITIVE_PROCESS_QUICK": ProfileToken("COGNITIVE_PROCESS_QUICK", order=10, non_overridable=True),
        ...         "HUMOR_PRESET_RANEVSKAYA": ProfileToken("HUMOR_PRESET_RANEVSKAYA", order=40),
        ...     }
        ... )
        >>> profile.blueprint_id
        'universal_agent_v1'
        >>> len(profile.tokens)
        2
    """

    blueprint_id: str
    tokens: Dict[str, ProfileToken] = field(default_factory=dict)

"""
Prompt component domain models.

Part of hexagonal architecture:
- Core domain objects (no dependencies on infrastructure)
- Immutable value objects
- Type-safe enums

Session: 23 (Prompt Component Architecture Implementation)
Session: 25 (Integration with 3-level priority system)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum

# ========================================================================
# SESSION_26: Execution Context Constants
# RFC: docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md
# Purpose: Required user_id/account_id for all operations (no Optional/None)
# ========================================================================

# Anonymous context (for unregistered users - first contact before registration)
ANONYMOUS_USER_ID = "anonymous"
ANONYMOUS_ACCOUNT_ID = "guest"

# System context (for background tasks, cron jobs, internal operations)
SYSTEM_USER_ID = "system"
SYSTEM_ACCOUNT_ID = "system"


class OwnerType(Enum):
    """
    Component ownership level for 4-level priority resolution.

    Priority order: USER > ACCOUNT > AGENT > SYSTEM

    SESSION_26: Added ACCOUNT level for multi-tenant prompt customization.

    NOTE: user_id and account_id are EXECUTION CONTEXT parameters.
    Domain treats them as opaque identifiers, adapters map to DB fields.
    See: docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md

    Examples:
        - SYSTEM: Default components for all agents
        - AGENT: Agent-specific overrides (quick/smart/router)
        - ACCOUNT: Account-level shared config (family/enterprise)
        - USER: User-specific customizations (highest priority)
    """
    SYSTEM = "SYSTEM"   # Default system components (lowest priority)
    AGENT = "AGENT"     # Agent-specific components
    ACCOUNT = "ACCOUNT" # Account-level shared config (NEW SESSION_26)
    USER = "USER"       # User customizations (highest priority)


class ComponentScope(Enum):
    """Where component lives in Groovy class hierarchy.
    
    All components belong to 'class Alek extends Agent'.
    Quick vs Smart agents differ by which scopes they include.
    """
    # Core components (used by Quick agent)
    CLASS_ROOT = "class.Alek"  # cognitive_process
    CLASS_PROPERTIES = "class.Alek.properties"  # properties block (archetype, humor_engine, etc)
    CLASS_POLICIES = "class.Alek.policies"  # policies block with @critical/@style rules
    
    # Extended components (Smart agent only)
    CLASS_KNOWLEDGE_BASE = "class.Alek.knowledge_base"  # few_shot_examples training data
    CLASS_PROTOCOLS = "class.Alek.protocols"  # protocols block (search_memory, web_search)
    CLASS_RUNTIME_RULES = "class.Alek.runtime_rules"  # runtime_rules block (Slack formatting, etc)


@dataclass(frozen=True)
class PromptComponent:
    """
    Immutable prompt component (building block).

    Each component represents a Groovy block that can be:
    - Loaded from defaults (SYSTEM level)
    - Overridden by agent (AGENT level - e.g., smart/quick)
    - Customized by account (ACCOUNT level - shared config)
    - Customized by user (USER level)
    - Assembled into final prompt

    Priority resolution: USER > ACCOUNT > AGENT > SYSTEM

    SESSION_25: Added 3-level ownership system
    SESSION_26: Extended to 4-level with ACCOUNT

    Ownership system:
    - owner_type: SYSTEM/AGENT/ACCOUNT/USER
    - owner_value: null for SYSTEM, agent_type for AGENT, account_id for ACCOUNT, user_id for USER
    - is_enabled: False = EXCLUDE pattern (component removed from assembly)

    Examples:
        - cognitive_process (SYSTEM): Default reasoning for all agents
        - cognitive_process (AGENT/smart): Smart-specific reasoning with tools
        - humor_engine (ACCOUNT/family123): Family account shared humor config
        - humor_engine (USER/user456): User personal override
    """
    id: str  # Unique identifier: "cognitive_process", "humor_engine"
    scope: ComponentScope  # Where in class hierarchy
    content: str  # Groovy code block (without scope wrapper)
    order: int  # Position within scope (for deterministic assembly)
    
    # SESSION_25: 3-level ownership system
    owner_type: OwnerType = OwnerType.SYSTEM  # Component ownership level
    owner_value: Optional[str] = None  # agent_type (quick/smart) or user_id
    is_enabled: bool = True  # False = exclude component from assembly
    
    # Legacy fields (kept for backward compatibility)
    is_user_override: bool = False  # Deprecated: use owner_type == USER
    version: str = "1.0"  # For migration tracking
    
    def __post_init__(self):
        """Validate component on creation."""
        if not self.id:
            raise ValueError("Component id required")
        # Note: content CAN be empty for fallthrough pattern (SESSION_24)
        # Empty content means "skip this level, try next priority level"


@dataclass
class PromptTemplate:
    """
    Template structure defining prompt assembly format and variable formatting.

    Defines:
    - Class name and inheritance (for Groovy format)
    - Which sections/scopes to include
    - Order of assembly
    - Output format (groovy/xml)
    - Variable formatting rules

    Examples:
        - TEMPLATE_LIGHT: Quick agent (minimal, no tools, Groovy format)
        - TEMPLATE_FULL: Smart agent (full features, tools, Groovy format)
        - TEMPLATE_CONSOLIDATION: Consolidation agent (XML format for Claude)
    """
    name: str  # "Alek" or "AlekWithTools" or "ConversationalUserProfiler"
    extends: Optional[str]  # "Agent" or "Alek" (for Groovy only)
    scopes: List[ComponentScope]  # Ordered list of sections
    supports_tools: bool  # Whether this template includes tool protocols

    # Output format control
    output_format: str = "groovy"  # "groovy" (with class wrapper) or "xml" (no wrapper)

    # Variable formatting rules
    variable_formats: Dict[str, str] = field(default_factory=dict)
    # Example: {
    #   "CONVERSATION_INPUT": "xml",      # Format conversation as XML
    #   "BIOGRAPHICAL_CONTEXT": "xml",    # Format facts as XML
    #   "EXISTING_ANCHORS": "json"        # Format anchors as JSON
    # }


# =============================================================================
# Predefined templates
# =============================================================================

TEMPLATE_LIGHT = PromptTemplate(
    name="Alek",
    extends="Agent",
    scopes=[
        ComponentScope.CLASS_PROPERTIES,     # properties (archetype, humor_engine, etc)
        ComponentScope.CLASS_POLICIES,       # policies (Output_Language_Protocol, etc)
        ComponentScope.CLASS_ROOT,           # cognitive_process
        ComponentScope.CLASS_KNOWLEDGE_BASE, # few_shot_examples + biographical_context
        ComponentScope.CLASS_PROTOCOLS,      # protocols (search_memory, web_search)
        ComponentScope.CLASS_RUNTIME_RULES,  # runtime_rules (Slack formatting)
    ],
    supports_tools=False
)

TEMPLATE_FULL = PromptTemplate(
    name="Alek",  # Same class name as Light, but with more components
    extends="Agent",
    scopes=[
        ComponentScope.CLASS_PROPERTIES,     # properties
        ComponentScope.CLASS_POLICIES,       # policies
        ComponentScope.CLASS_ROOT,           # cognitive_process
        ComponentScope.CLASS_KNOWLEDGE_BASE, # few_shot_examples
        ComponentScope.CLASS_PROTOCOLS,      # protocols (search_memory, web_search)
        ComponentScope.CLASS_RUNTIME_RULES,  # runtime_rules (Slack formatting)
    ],
    supports_tools=True
)

# =============================================================================
# Minimal templates for specialized agents (JSON output only)
# =============================================================================

TEMPLATE_WEBSEARCH = PromptTemplate(
    name="SearchAgent",
    extends="GoogleSearchAgent",
    scopes=[
        ComponentScope.CLASS_ROOT,           # cognitive_process only
        ComponentScope.CLASS_KNOWLEDGE_BASE, # biographical_context (for location awareness)
    ],
    supports_tools=False
)

TEMPLATE_ROUTER = PromptTemplate(
    name="TriageAgent",
    extends="Agent",
    scopes=[
        ComponentScope.CLASS_ROOT,           # cognitive_process only
    ],
    supports_tools=False
)

TEMPLATE_CONSOLIDATION = PromptTemplate(
    name="ConversationalUserProfiler",
    extends="Agent",
    scopes=[
        ComponentScope.CLASS_ROOT,           # cognitive_process only
    ],
    supports_tools=False,
    output_format="groovy",  # Groovy DSL format with class wrapper
    variable_formats={
        "CONVERSATION_INPUT": "xml",      # Format conversation as XML
        "BIOGRAPHICAL_CONTEXT": "xml",    # Format biographical facts as XML
        "EXISTING_ANCHORS": "json"        # Format anchors as JSON list
    }
)

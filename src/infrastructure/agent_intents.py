"""
Agent Intents
=============
Shared intent filter for agent delegation.

DEFAULT_INTENTS is the canonical set of intents exposed to both Quick and Smart agents.
It excludes implementation-internal intents (e.g. search_web_light) that exist in the
AgentRegistry only for coordinator routing — they are not decision-making options for LLMs.

Quick uses DEFAULT_INTENTS as-is, then remaps search_web → search_web_light at dispatch time
via _INTENT_REMAP. Smart uses DEFAULT_INTENTS unmodified (full web search, no remap needed).
"""

DEFAULT_INTENTS: frozenset = frozenset(
    {
        "search_memory",
        "search_web",
        "search_emails",
        "get_email_details",
        "get_email_attachment",
    }
)

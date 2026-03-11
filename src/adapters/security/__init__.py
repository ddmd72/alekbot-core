"""
Security Adapters - SecurityPort implementations.

Extensible validation architecture with multiple adapters:
- RegexSecurityAdapter: Pattern-based validation (MVP)
- LLMSecurityAdapter: LLM-based semantic risk assessment (placeholder)
- ExternalAPIAdapter: External service validation (placeholder)
- CompositeAdapter: Aggregates multiple adapters (MVP)
"""

from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.adapters.security.composite_adapter import CompositeAdapter
from src.adapters.security.llm_adapter import LLMSecurityAdapter
from src.adapters.security.external_api_adapter import ExternalAPIAdapter

__all__ = [
    "RegexSecurityAdapter",
    "CompositeAdapter",
    "LLMSecurityAdapter",
    "ExternalAPIAdapter",
]

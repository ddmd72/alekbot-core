"""
Multi-Agent System
==================

Agent-based architecture for specialized task handling.
Each agent is a self-contained unit with specific capabilities.
"""

from .base_agent import BaseAgent, CircuitBreaker
from .memory_search_agent import FactsMemoryAgent
from .web_search_agent import WebSearchAgent
from .consolidation_agent import ConsolidationAgent

__all__ = [
    "BaseAgent",
    "CircuitBreaker",
    "FactsMemoryAgent",
    "WebSearchAgent",
    "ConsolidationAgent",
]

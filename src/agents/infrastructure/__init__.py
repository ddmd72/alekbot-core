"""
Infrastructure Agents
=====================

Cross-cutting agents for system concerns like billing.
"""

from .billing_agent import BillingAgent

__all__ = ["BillingAgent"]

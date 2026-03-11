# Backward-compat re-export. Logic lives in domain/billing.py.
from ..domain.billing import calculate_cost, _PRICING_PER_MILLION_TOKENS as PRICING_PER_MILLION_TOKENS

__all__ = ["calculate_cost", "PRICING_PER_MILLION_TOKENS"]

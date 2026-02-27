"""
Prompt Design System v4 — Domain Models

Token-based prompt architecture with class-collection assembly.
RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md
"""

from src.domain.prompt_v3.token import Token, TokenId, TokenCategory
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.security import (
    SecurityPort,
    ValidationResult,
    RiskLevel,
    TrustZone,
)

__all__ = [
    "Token",
    "TokenId",
    "TokenCategory",
    "OwnerType",
    "Blueprint",
    "ProfileToken",
    "SecurityPort",
    "ValidationResult",
    "RiskLevel",
    "TrustZone",
]

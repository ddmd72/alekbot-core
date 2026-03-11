"""
Auth domain models — OAuth/OIDC data structures and authorization value objects.

Moved from src/ports/auth_port.py and src/ports/platform_auth_port.py (TD-V2, 2026-03-08).
Ports/ contains only ABCs; data models belong in domain/.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any

from pydantic import BaseModel

from src.domain.user import UserProfile


class TokenClaims(BaseModel):
    """Claims extracted from OAuth token (JWT). Based on OIDC standard claims."""

    sub: str  # Subject - unique user identifier from provider
    iss: str  # Issuer - provider identifier
    aud: str  # Audience - client ID
    exp: datetime  # Expiration time
    iat: datetime  # Issued at time

    # Optional OIDC standard claims
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    name: Optional[str] = None
    picture: Optional[str] = None

    # Provider-specific claims (stored as-is)
    custom_claims: Dict[str, Any] = {}


class OAuthTokens(BaseModel):
    """OAuth tokens received after authorization code exchange."""

    access_token: str
    refresh_token: Optional[str] = None
    id_token: str  # JWT with user claims
    expires_in: int  # Seconds until access_token expires
    token_type: str = "Bearer"


class OAuthUserInfo(BaseModel):
    """User information from OAuth provider's UserInfo endpoint."""

    # OIDC standard fields
    sub: str  # Subject identifier (unique user ID)
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    name: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    picture: Optional[str] = None
    locale: Optional[str] = None

    # Provider-specific metadata
    provider_metadata: Dict[str, Any] = {}


@dataclass
class IAMDecision:
    """Authorization decision from platform auth checks.

    action: "allow" | "reject" | "create_account"
    user: Resolved UserProfile when action == "allow".
    message: User-facing rejection message when action == "reject".
    metadata: Additional context (e.g., platform_user_id for display).
    """

    action: str
    user: Optional[UserProfile] = None
    message: Optional[str] = None
    metadata: dict = field(default_factory=dict)

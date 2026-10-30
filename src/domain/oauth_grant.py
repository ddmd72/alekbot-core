"""What an OAuth edge's code exchange returns (MULTI_DRIVE_RFC §5.1): the tokens, the granted scopes and the
consenting provider account."""
from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

from .provider_account import ProviderAccount


@dataclass(frozen=True)
class GrantResult:
    access_token: str
    refresh_token: str
    token_expiry: datetime
    scopes: Tuple[str, ...]
    account: ProviderAccount

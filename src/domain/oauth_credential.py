"""One stored grant (MULTI_DRIVE_RFC §3, §4.1; SECRETS_AT_REST_RFC §2.3).

`credential_id` is opaque and chosen by the store on the first save, never by a caller ("" before it). `kind` is the
connector kind of the injected `ConnectorSpec` (gmail, google_tasks, microsoft_todo, microsoft_onedrive). `account`
is the consenting provider account; its `label` and the tokens are ciphertext at rest, everything else is plaintext.

`integration` and `account_id` are `Optional` on purpose: a record written before they existed reads with `None`, so
the missing value is visible until the backfill sets it; the store never derives them. A consent refuses a credential
without them.

`needs_reconnect` is set when the provider rejected the refresh token; only a new consent clears it (a successful
refresh is the proof it no longer holds).

"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from .integration import Integration
from .provider_account import ProviderAccount


@dataclass(kw_only=True)
class OAuthCredential:
    credential_id: str = ""
    user_id: str
    account_id: Optional[str]
    kind: str
    integration: Optional[Integration]
    account: ProviderAccount
    access_token: str
    refresh_token: str
    token_expiry: datetime
    scopes: List[str]
    needs_reconnect: bool = False

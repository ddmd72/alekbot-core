"""The key of one stored credential (MULTI_DRIVE_RFC §4.2): what `OAuthCredentialsPort.list_by_account` returns, across
users, without opening any secret."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialKey:
    user_id: str
    credential_id: str

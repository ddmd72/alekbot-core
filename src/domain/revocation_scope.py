"""What a provider-side revoke ends (MULTI_DRIVE_RFC §4.3): `GRANT_WIDE` (the grant of the client and the account, as
Google's) or `NONE` (the realm is never revoked at the provider, as Microsoft)."""
from enum import Enum


class RevocationScope(str, Enum):
    GRANT_WIDE = "grant_wide"
    NONE = "none"

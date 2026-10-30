"""The provider account a grant belongs to (MULTI_DRIVE_RFC §3): `realm` google | microsoft, `subject` the provider's
stable account id (Google `sub`, Graph `/me.id`), `label` its display name (personal: stored inside the ciphertext)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderAccount:
    realm: str
    subject: str
    label: str

"""A server-side connect flow (MULTI_DRIVE_RFC §5.2), written at start and consumed once by the callback.

`target_id` is the connection or credential a reconnect targets ("" otherwise); `expected_subject` the account a
reconnect must come back with ("" otherwise); `hint_label` the account label the edge may send as a login hint.
The raw OAuth `state` is not a field: the persisted flow is keyed by its keyed digest only. `pkce_verifier` is
plaintext here; the store keeps it as ciphertext.
"""
from dataclasses import dataclass
from datetime import datetime

from .flow_intent import FlowIntent


@dataclass(frozen=True)
class OAuthFlow:
    user_id: str
    kind: str
    intent: FlowIntent
    target_id: str
    expected_subject: str
    hint_label: str
    pkce_verifier: str
    return_path: str
    expires_at: datetime

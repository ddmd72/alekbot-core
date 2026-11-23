"""DeviceSessionPolicy.change_proof_fresh (CABINET_DEVICE_SESSIONS_RFC §7.3): changing the passkeys is allowed after a
fresh passkey assertion OR a fresh Google sign-in on the session, each under the 5-minute window."""
from datetime import timedelta

import pytest

from src.domain.device_session import SessionAuthKind
from src.domain.device_session_policy import DeviceSessionPolicy as P
from tests.unit.domain.test_device_session_policy import T0, session

GRACE = P.CALL_PASSKEY_GRACE
GOOGLE, PASSKEY = SessionAuthKind.GOOGLE, SessionAuthKind.PASSKEY


@pytest.mark.parametrize("kind, auth_at, passkey_at, now, allowed", [
    (GOOGLE, T0, T0, T0, True),                                              # both proofs fresh
    (GOOGLE, T0 - timedelta(days=1), T0, T0, True),                          # passkey step-up on an old Google session
    (GOOGLE, T0, None, T0, True),                                            # fresh Google sign-in
    (GOOGLE, T0, None, T0 + GRACE - timedelta(microseconds=1), True),        # last instant
    (GOOGLE, T0, None, T0 + GRACE, False),                                   # Google proof stale (end exclusive)
    (GOOGLE, T0 - timedelta(days=1), None, T0, False),                       # no proof at all
    (PASSKEY, T0, T0, T0, True),                                             # passkey sign-in is itself an assertion
    (PASSKEY, T0, T0, T0 + GRACE, False),                                    # a passkey session with an old auth_at
    (PASSKEY, T0, None, T0, False),                                          # auth_at alone proves nothing for passkey
], ids=["both", "google-old-passkey-fresh", "google-fresh", "google-last-instant", "google-stale", "no-proof",
        "passkey-fresh", "passkey-session-old", "passkey-session-no-assertion"])
def test_change_proof_fresh(kind, auth_at, passkey_at, now, allowed):
    assert P.change_proof_fresh(session(auth_kind=kind, auth_at=auth_at, passkey_at=passkey_at), now) is allowed

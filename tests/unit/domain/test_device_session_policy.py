"""DeviceSessionPolicy (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.2): the lifetimes, liveness at each boundary (every end is
exclusive) and the refresh decision table."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy as P, RefreshOutcome, refresh_secret_hash

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
CURRENT, PREVIOUS, OTHER = refresh_secret_hash("current"), refresh_secret_hash("previous"), refresh_secret_hash("x")
ROTATED_AT = T0 + timedelta(days=1)


def session(**over) -> DeviceSession:
    base = dict(sid="sid-1", user_id="user-alex", account_id="acct-alex", label="Mac — Chrome",
                platform_hint="Mac — Chrome", auth_kind=SessionAuthKind.GOOGLE, auth_at=T0, created_at=T0,
                last_seen_at=ROTATED_AT, expires_at=P.absolute_end(T0), refresh_hash=CURRENT,
                prev_refresh_hash=PREVIOUS, rotated_at=ROTATED_AT, purge_after=P.purge_after(ROTATED_AT))
    base.update(over)
    return DeviceSession(**base)


def test_the_lifetimes_are_the_rfc_numbers():
    assert (P.ACCESS_TTL, P.IDLE_TTL, P.ABSOLUTE_TTL, P.ROTATION_GRACE, P.GATE_CACHE_TTL, P.PURGE_AFTER) == (
        timedelta(minutes=15), timedelta(days=14), timedelta(days=30), timedelta(seconds=60), timedelta(seconds=30),
        timedelta(days=30))
    assert P.access_expiry(T0) == T0 + timedelta(minutes=15)
    assert P.absolute_end(T0) == datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)
    assert P.purge_after(T0) == datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


def test_the_secret_hash_is_sha256_hex_of_the_secret_text():
    assert refresh_secret_hash("abc") == hashlib.sha256(b"abc").hexdigest()
    assert refresh_secret_hash("abc") != refresh_secret_hash("abd")


IDLE_END = ROTATED_AT + timedelta(days=14)
ABSOLUTE_END = T0 + timedelta(days=30)


@pytest.mark.parametrize("record, now, live", [
    (session(), ROTATED_AT, True),
    (session(), IDLE_END - timedelta(microseconds=1), True),
    (session(), IDLE_END, False),                                                   # exactly at the idle end
    (session(last_seen_at=T0 + timedelta(days=20)), ABSOLUTE_END - timedelta(microseconds=1), True),
    (session(last_seen_at=T0 + timedelta(days=20)), ABSOLUTE_END, False),           # exactly at the absolute end
    (session(revoked_at=ROTATED_AT, revoke_reason=SessionRevokeReason.LOGOUT), ROTATED_AT, False),
], ids=["fresh", "just-before-idle-end", "at-idle-end", "just-before-absolute-end", "at-absolute-end", "revoked"])
def test_is_live_at_each_boundary(record, now, live):
    assert P.is_live(record, now) is live


GRACE_END = ROTATED_AT + timedelta(seconds=60)


@pytest.mark.parametrize("record, presented, now, outcome", [
    (session(), CURRENT, ROTATED_AT + timedelta(hours=1), RefreshOutcome.ROTATE),
    (session(prev_refresh_hash=None, rotated_at=None), CURRENT, ROTATED_AT, RefreshOutcome.ROTATE),   # never rotated
    (session(), PREVIOUS, ROTATED_AT, RefreshOutcome.GRACE),
    (session(), PREVIOUS, GRACE_END - timedelta(microseconds=1), RefreshOutcome.GRACE),
    (session(), PREVIOUS, GRACE_END, RefreshOutcome.REPLAY),                      # exactly at the grace end
    (session(), PREVIOUS, ROTATED_AT + timedelta(days=2), RefreshOutcome.REPLAY),
    (session(), OTHER, ROTATED_AT, RefreshOutcome.REPLAY),                        # matches neither
    (session(prev_refresh_hash=None, rotated_at=None), OTHER, ROTATED_AT, RefreshOutcome.REPLAY),
    (session(rotated_at=None), PREVIOUS, ROTATED_AT, RefreshOutcome.REPLAY),     # a previous hash with no rotation time
    (session(revoked_at=ROTATED_AT, revoke_reason=SessionRevokeReason.KILL_SWITCH), CURRENT, ROTATED_AT,
     RefreshOutcome.DEAD),
    (session(), CURRENT, IDLE_END, RefreshOutcome.DEAD),                          # idle end, the right secret
    (session(last_seen_at=T0 + timedelta(days=20)), CURRENT, ABSOLUTE_END, RefreshOutcome.DEAD),
    (session(), PREVIOUS, IDLE_END, RefreshOutcome.DEAD),                         # dead beats replay: nothing to revoke
], ids=["rotate", "rotate-first", "grace", "grace-last-instant", "replay-at-grace-end", "replay-later",
        "replay-neither", "replay-neither-unrotated", "replay-no-rotation-time", "dead-revoked", "dead-idle",
        "dead-absolute", "dead-old-secret"])
def test_decide_refresh(record, presented, now, outcome):
    assert P.decide_refresh(record, presented, now) is outcome


@pytest.mark.parametrize("passkey_at, now, fresh", [
    (None, T0, False),                                              # no assertion ever
    (T0, T0, True),
    (T0, T0 + timedelta(minutes=5) - timedelta(microseconds=1), True),
    (T0, T0 + timedelta(minutes=5), False),                         # the grace end is exclusive
    (T0 + timedelta(seconds=2), T0, True),                          # an instance whose clock runs a little behind
], ids=["never", "just-now", "last-instant", "grace-end", "clock-behind"])
def test_passkey_fresh_is_an_assertion_under_five_minutes_old(passkey_at, now, fresh):
    assert P.CALL_PASSKEY_GRACE == timedelta(minutes=5)
    assert P.passkey_fresh(session(passkey_at=passkey_at), now) is fresh

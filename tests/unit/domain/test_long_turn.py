from src.domain.long_turn import LongTurnRecord, LongTurnStatus, RetryVerdict


def _rec(**kw):
    base = dict(turn_id="slack:Ev1", user_id="u1", session_id="u1:D1", title="t",
                started_at=0.0, heartbeat_at=100.0)
    base.update(kw)
    return LongTurnRecord(**base)


def test_running_with_fresh_heartbeat_is_running():
    assert _rec().verdict(now=150.0, stale_after_s=90) is RetryVerdict.RUNNING


def test_running_with_stale_heartbeat_is_stale():
    assert _rec().verdict(now=191.0, stale_after_s=90) is RetryVerdict.STALE


def test_any_terminal_status_is_finished():
    for status in (LongTurnStatus.DONE, LongTurnStatus.FAILED, LongTurnStatus.CANCELLED):
        assert _rec(status=status).verdict(now=10_000.0, stale_after_s=90) is RetryVerdict.FINISHED

"""FactsPinPolicy (CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8): which codes are refused, what one attempt decides and
leaves in the counter, which lock it writes, and the unlock and reset instants (every end exclusive)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.device_session import DeviceSession, SessionAuthKind
from src.domain.device_session_policy import DeviceSessionPolicy
from src.domain.facts_pin import FactsPin
from src.domain.facts_pin_policy import CodeProblem, FactsPinPolicy as P, PinOutcome, PinVerdict
from src.domain.user import InactiveReason

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
PIN_D, DURESS_D, OTHER_D = "a" * 64, "b" * 64, "c" * 64


def record(wrong: int = 0, **over) -> FactsPin:
    return FactsPin(**{"user_id": "user-alex", "pin_digest": PIN_D, "duress_digest": DURESS_D, "wrong_in_row": wrong,
                       "updated_at": T0, **over})


def session(**over) -> DeviceSession:
    base = dict(sid="sid-1", user_id="user-alex", account_id="acct-alex", label="Mac — Chrome",
                platform_hint="Mac — Chrome", auth_kind=SessionAuthKind.GOOGLE, auth_at=T0, created_at=T0,
                last_seen_at=T0, expires_at=DeviceSessionPolicy.absolute_end(T0), refresh_hash="h",
                purge_after=DeviceSessionPolicy.purge_after(T0))
    base.update(over)
    return DeviceSession(**base)


class TestCodes:
    @pytest.mark.parametrize("code", ["0001", "0002"])
    def test_ordinary_codes_are_not_trivial(self, code):
        assert P.is_trivial(code) is False

    @pytest.mark.parametrize("code", [
        "0000", "7777",                      # one digit
        "1234", "6789", "7890", "8901", "9012",    # ascending, wrapping from 9 to 0
        "4321", "3210", "2109", "1098", "0987",    # descending, wrapping from 0 to 9
        "1212", "4545", "0909",              # a repeated pair
        "1004", "2000", "2001", "1122", "2580", "0852",   # the common list
    ])
    def test_trivial_codes(self, code):
        assert P.is_trivial(code) is True

    @pytest.mark.parametrize("code", ["000", "00011", "", "00a1", "0 01", "٠٠٠١", "０００１"])
    def test_only_four_ascii_digits_are_well_formed(self, code):
        assert P.is_well_formed(code) is False
        assert P.code_problem(code, "0002") is CodeProblem.NOT_FOUR_DIGITS
        assert P.code_problem("0001", code) is CodeProblem.NOT_FOUR_DIGITS

    def test_a_good_pair_has_no_problem(self):
        assert P.is_well_formed("0001") is True
        assert P.code_problem("0001", "0002") is None

    def test_either_code_trivial_is_refused_and_the_duress_code_differs(self):
        assert P.code_problem("1234", "0002") is CodeProblem.TRIVIAL
        assert P.code_problem("0001", "1111") is CodeProblem.TRIVIAL
        assert P.code_problem("0001", "0001") is CodeProblem.DURESS_SAME_AS_PIN


class TestAttempt:
    def test_no_record_is_no_pin(self):
        assert P.decide(None, True, PIN_D, DURESS_D) == PinOutcome(PinVerdict.NO_PIN)

    def test_an_inactive_user_is_locked_and_nothing_counts(self):
        stored = record(wrong=2)
        outcome = P.decide(stored, False, PIN_D, DURESS_D)
        assert outcome == PinOutcome(PinVerdict.LOCKED) and outcome.lock_reason is None
        assert P.counter_after(stored, outcome) == 2

    def test_the_duress_code_locks_and_leaves_the_counter(self):
        stored = record(wrong=3)
        outcome = P.decide(stored, True, OTHER_D, DURESS_D)
        assert outcome == PinOutcome(PinVerdict.DURESS, locked_now=True)
        assert outcome.lock_reason is InactiveReason.DURESS
        assert P.counter_after(stored, outcome) == 3

    def test_the_pin_clears_the_counter(self):
        stored = record(wrong=4)
        outcome = P.decide(stored, True, PIN_D, OTHER_D)
        assert outcome == PinOutcome(PinVerdict.OK) and outcome.lock_reason is None
        assert P.counter_after(stored, outcome) == 0

    @pytest.mark.parametrize("wrong, left", [(0, 4), (1, 3), (2, 2), (3, 1)])
    def test_a_wrong_code_counts_and_says_what_is_left(self, wrong, left):
        stored = record(wrong=wrong)
        outcome = P.decide(stored, True, OTHER_D, OTHER_D)
        assert outcome == PinOutcome(PinVerdict.WRONG, attempts_left=left) and outcome.lock_reason is None
        assert P.counter_after(stored, outcome) == wrong + 1

    @pytest.mark.parametrize("wrong", [4, 6])
    def test_the_fifth_wrong_code_in_a_row_locks(self, wrong):
        stored = record(wrong=wrong)
        outcome = P.decide(stored, True, OTHER_D, OTHER_D)
        assert outcome == PinOutcome(PinVerdict.LOCKED, locked_now=True)
        assert outcome.lock_reason is InactiveReason.PIN_ATTEMPTS
        assert P.counter_after(stored, outcome) == wrong + 1

    def test_a_no_pin_outcome_leaves_any_counter(self):
        assert P.counter_after(record(wrong=2), PinOutcome(PinVerdict.NO_PIN)) == 2


class TestUnlockAndReset:
    def test_the_unlock_lasts_fifteen_minutes_and_its_end_is_exclusive(self):
        end = P.unlock_end(T0)
        assert end == T0 + timedelta(minutes=15)
        unlocked = session(facts_unlocked_until=end)
        assert P.is_unlocked(unlocked, end - timedelta(microseconds=1)) is True
        assert P.is_unlocked(unlocked, end) is False

    def test_a_never_unlocked_or_dead_session_is_not_unlocked(self):
        assert P.is_unlocked(session(), T0) is False
        assert P.is_unlocked(session(facts_unlocked_until=T0 + timedelta(hours=1), revoked_at=T0), T0) is False

    def test_a_reset_needs_a_google_sign_in_under_five_minutes_on_a_live_session(self):
        fresh = session()
        assert P.may_request_reset(fresh, T0 + timedelta(minutes=5) - timedelta(microseconds=1)) is True
        assert P.may_request_reset(fresh, T0 + timedelta(minutes=5)) is False
        assert P.may_request_reset(session(auth_kind=SessionAuthKind.PASSKEY), T0) is False
        assert P.may_request_reset(session(revoked_at=T0), T0) is False

    def test_a_reset_is_due_twenty_four_hours_later(self):
        due = P.reset_due(T0)
        assert due == T0 + timedelta(hours=24)
        pending = record(reset_requested_at=T0, reset_due_at=due)
        assert P.reset_is_due(pending, due - timedelta(microseconds=1)) is False
        assert P.reset_is_due(pending, due) is True
        assert P.reset_is_due(record(), due) is False

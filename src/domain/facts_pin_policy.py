"""FactsPinPolicy — the rules of the facts PIN and the duress code (CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8, §13).
Pure: every instant and digest is passed in.

- **Codes.** Exactly four ASCII digits. A trivial code is refused: one digit four times, an ascending or descending
  run (wrapping from 9 to 0 counts: 7890, 2109), a repeated pair (4545), or one of a short list of the most chosen
  codes the rules above miss. The duress code obeys the same rules and differs from the PIN.
- **An attempt** (`decide`): the duress code locks the account (`DURESS`); the PIN answers `OK` and clears the counter;
  anything else counts one more wrong code: `WRONG` with the attempts left, and at `MAX_WRONG` in a row the account
  locks (`LOCKED`). A user without a record gets `NO_PIN`, nothing counted. A user already inactive gets `LOCKED` with
  nothing written: the lock fires once. `locked_now` says the attempt's own transaction wrote the lock.
- **The unlock** of a session lasts `UNLOCK_TTL`; leaving the tab relocks it earlier (best effort, the TTL is the
  guarantee). Every end is exclusive.
- **A reset** (a forgotten PIN) needs a Google sign-in less than `RESET_SIGN_IN_MAX_AGE` old on a live session, and
  takes effect `RESET_DELAY` after it was asked for.

This module holds the vocabulary of an attempt and of a code change (`PinVerdict`, `PinOutcome`, `PinSurface`,
`CodeProblem`, `PinChangeStatus`, `PinChange`) next to the policy on purpose: they have no meaning apart from it.
"""
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import ClassVar, FrozenSet, Optional

from .device_session import DeviceSession, SessionAuthKind
from .device_session_policy import DeviceSessionPolicy
from .facts_pin import FactsPin
from .user import InactiveReason


class PinVerdict(str, Enum):
    OK = "ok"
    WRONG = "wrong"
    DURESS = "duress"
    LOCKED = "locked"
    NO_PIN = "no_pin"


class PinSurface(str, Enum):
    """Where a code was entered: the Cabinet's facts tab, or a `$<digits>` chat command (§7.9). Logged only."""
    CABINET = "cabinet"
    CHAT = "chat"


class CodeProblem(str, Enum):
    """Why a code offered as a new PIN or duress code is refused."""
    NOT_FOUR_DIGITS = "not_four_digits"
    TRIVIAL = "trivial"
    DURESS_SAME_AS_PIN = "duress_same_as_pin"


@dataclass(frozen=True)
class PinOutcome:
    """The answer to one attempt. `attempts_left` is set for `WRONG` only; `locked_now` is true for a `DURESS` or
    `LOCKED` whose own transaction wrote the account lock (false when the account was locked already)."""
    verdict: PinVerdict
    attempts_left: Optional[int] = None
    locked_now: bool = False

    @property
    def lock_reason(self) -> Optional[InactiveReason]:
        """The `inactive_reason` this attempt wrote, or None when it wrote no lock."""
        if not self.locked_now:
            return None
        return InactiveReason.DURESS if self.verdict is PinVerdict.DURESS else InactiveReason.PIN_ATTEMPTS


class PinChangeStatus(str, Enum):
    """How setting, changing or removing the codes ended."""
    DONE = "done"
    CODES_REFUSED = "codes_refused"     # the new codes break a rule (`PinChange.problem`)
    ALREADY_SET = "already_set"         # a first PIN for a user who has one: it is changed, with the current PIN
    NO_PIN = "no_pin"                   # a change or removal for a user without a PIN
    NOT_VERIFIED = "not_verified"       # the current PIN was not right (`PinChange.attempt`: WRONG, DURESS or LOCKED)


@dataclass(frozen=True)
class PinChange:
    status: PinChangeStatus
    problem: Optional[CodeProblem] = None
    attempt: Optional[PinOutcome] = None


def _same(stored: str, presented: str) -> bool:
    return hmac.compare_digest(stored, presented)


class FactsPinPolicy:
    CODE_LENGTH: ClassVar[int] = 4
    MAX_WRONG: ClassVar[int] = 5                                          # the fifth wrong code in a row locks
    UNLOCK_TTL: ClassVar[timedelta] = timedelta(minutes=15)               # one unlock of the facts tab (§13 decision 2)
    RESET_DELAY: ClassVar[timedelta] = timedelta(hours=24)                # a forgotten PIN's reset waits this long
    RESET_SIGN_IN_MAX_AGE: ClassVar[timedelta] = timedelta(minutes=5)     # the Google sign-in a reset asks for
    # The most chosen four-digit codes that the run, repeat and pair rules do not already refuse (the top of the
    # DataGenetics 2012 PIN frequency table), and the keypad's middle column both ways.
    COMMON_CODES: ClassVar[FrozenSet[str]] = frozenset({"1004", "2000", "2001", "1122", "2580", "0852"})

    # ------------------------------------------------------------------ codes

    @classmethod
    def is_well_formed(cls, code: str) -> bool:
        """Exactly `CODE_LENGTH` ASCII digits (a full-width or Arabic-Indic digit is not one)."""
        return len(code) == cls.CODE_LENGTH and all(c in "0123456789" for c in code)

    @classmethod
    def is_trivial(cls, code: str) -> bool:
        """Whether a well-formed code is one a thief tries first: one digit repeated, a run up or down (wrapping from 9
        to 0), a repeated pair, or a common code."""
        digits = [int(c) for c in code]
        if len(set(digits)) == 1:
            return True
        steps = {(b - a) % 10 for a, b in zip(digits, digits[1:])}
        if steps in ({1}, {9}):
            return True
        if code[:2] == code[2:]:
            return True
        return code in cls.COMMON_CODES

    @classmethod
    def code_problem(cls, pin: str, duress: str) -> Optional[CodeProblem]:
        """Why the pair offered as a new PIN and duress code is refused, or None when both may be set."""
        for code in (pin, duress):
            if not cls.is_well_formed(code):
                return CodeProblem.NOT_FOUR_DIGITS
        for code in (pin, duress):
            if cls.is_trivial(code):
                return CodeProblem.TRIVIAL
        if pin == duress:
            return CodeProblem.DURESS_SAME_AS_PIN
        return None

    # ------------------------------------------------------------------ an attempt

    @classmethod
    def decide(cls, record: Optional[FactsPin], user_active: bool, pin_digest: str,
               duress_digest: str) -> PinOutcome:
        """What an attempt whose code digests (once as the PIN, once as the duress code) are `pin_digest` and
        `duress_digest` does, given the stored record and whether the user is active now."""
        if record is None:
            return PinOutcome(PinVerdict.NO_PIN)
        if not user_active:
            return PinOutcome(PinVerdict.LOCKED)
        if _same(record.duress_digest, duress_digest):
            return PinOutcome(PinVerdict.DURESS, locked_now=True)
        if _same(record.pin_digest, pin_digest):
            return PinOutcome(PinVerdict.OK)
        wrong = record.wrong_in_row + 1
        if wrong >= cls.MAX_WRONG:
            return PinOutcome(PinVerdict.LOCKED, locked_now=True)
        return PinOutcome(PinVerdict.WRONG, attempts_left=cls.MAX_WRONG - wrong)

    @staticmethod
    def counter_after(record: FactsPin, outcome: PinOutcome) -> int:
        """The `wrong_in_row` the attempt leaves: cleared by the PIN, one more for a wrong code (the locking one
        included), unchanged by the duress code or an attempt on a locked account."""
        if outcome.verdict is PinVerdict.OK:
            return 0
        if outcome.verdict is PinVerdict.WRONG or (outcome.verdict is PinVerdict.LOCKED and outcome.locked_now):
            return record.wrong_in_row + 1
        return record.wrong_in_row

    # ------------------------------------------------------------------ unlock and reset

    @classmethod
    def unlock_end(cls, now: datetime) -> datetime:
        """`facts_unlocked_until` of an unlock at `now`."""
        return now + cls.UNLOCK_TTL

    @staticmethod
    def is_unlocked(session: DeviceSession, now: datetime) -> bool:
        """The session is live and its unlock has not ended."""
        return (session.facts_unlocked_until is not None and now < session.facts_unlocked_until
                and DeviceSessionPolicy.is_live(session, now))

    @classmethod
    def may_request_reset(cls, session: DeviceSession, now: datetime) -> bool:
        """The session is live and was signed in with Google less than `RESET_SIGN_IN_MAX_AGE` ago."""
        return (session.auth_kind is SessionAuthKind.GOOGLE and now - session.auth_at < cls.RESET_SIGN_IN_MAX_AGE
                and DeviceSessionPolicy.is_live(session, now))

    @classmethod
    def reset_due(cls, requested_at: datetime) -> datetime:
        """`reset_due_at` of a reset asked for at `requested_at`."""
        return requested_at + cls.RESET_DELAY

    @staticmethod
    def reset_is_due(record: FactsPin, now: datetime) -> bool:
        """A reset is pending and its time has come."""
        return record.reset_due_at is not None and now >= record.reset_due_at

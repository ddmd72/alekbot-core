"""The chat side of the facts PIN (CABINET_DEVICE_SESSIONS_RFC §7.9): which `$` commands are a code, and the fixed reply
each attempt gets. Pure.

A `$` command made of digits only is a code (`is_code_command`), whatever its length: it never reaches the unknown-
command reply, which would echo it. Its reply (`reply_for`) never carries the code:
- the duress code: the lock notice of the duress code;
- the fifth wrong code in a row: the lock notice of the attempts;
- the real PIN: it does nothing in chat, it now sits in this chat's history, change it in the Cabinet;
- another code: "Wrong code" with the attempts left;
- an account already locked (a second message racing the first): the plain lock line;
- a user without a PIN: the fixed "no facts PIN is set" line (nothing was counted).
"""
from typing import Dict, Tuple

from .facts_pin_policy import PinOutcome, PinVerdict
from .ui_messages import UIMessage


def is_code_command(command: str) -> bool:
    """True for a `$` command (already stripped of `$` and spaces) made of digits only. Any Unicode digit counts, so a
    full-width or Arabic-Indic code is answered as a code too (a wrong one) and never echoed."""
    return command.isdigit()


def reply_for(outcome: PinOutcome) -> Tuple[UIMessage, Dict[str, str]]:
    """The fixed UI string, with its fields, answering one chat attempt."""
    if outcome.verdict is PinVerdict.NO_PIN:
        return UIMessage.FACTS_PIN_CHAT_NO_PIN, {}
    if outcome.verdict is PinVerdict.OK:
        return UIMessage.FACTS_PIN_CHAT_REAL_PIN, {}
    if outcome.verdict is PinVerdict.WRONG:
        return UIMessage.FACTS_PIN_CHAT_WRONG, {"left": str(outcome.attempts_left)}
    if outcome.verdict is PinVerdict.DURESS:
        return UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE, {}
    if outcome.locked_now:
        return UIMessage.ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE, {}
    return UIMessage.FACTS_PIN_CHAT_LOCKED, {}

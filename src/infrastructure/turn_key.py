"""
The default source of an orchestrator's turn key (docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md §4.1, §4.8): three
words of `WORDS` drawn with `secrets`. Randomness is not domain, so it lives here, where Smart and Quick may import it
(agents/ → infrastructure/ is allowed; infrastructure/ → domain/ only). Tests inject their own source instead.
"""
from __future__ import annotations

import secrets
from typing import Callable

from ..domain.system_fingerprint import WORDS

TurnKeySource = Callable[[], str]


def draw_turn_key() -> str:
    """A fresh key: three words of `WORDS`, each drawn independently with `secrets.choice`, joined by `-`."""
    return "-".join(secrets.choice(WORDS) for _ in range(3))

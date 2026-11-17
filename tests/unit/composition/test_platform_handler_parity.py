"""The Slack and the Telegram `ConversationHandler` of the production graph hold the same dependencies (composition-root
assessment D2: the handler's ~20 keywords are copied by hand into each platform's wiring, and P9-D1, a
`doc_delivery_service` dropped from both handlers, is that failure).

The graph is the `prod_graph` fixture (`tests/support/prod_graph.py`, the deploy's sealed environment over fakes). The
check compares the names of the non-`None` instance attributes (`vars(handler)`) of the two handlers. A difference is
allowed only when it is declared: `PLATFORM_ONLY` for a difference that is the platform's nature or an owner ruling,
`DRIFT` for one the composition-root refactor removes. Both fail both ways: an undeclared difference fails, and a
declared one that is gone fails: delete its entry.
"""
from typing import Dict, Set

import pytest

from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)

PLATFORM_ONLY: Dict[str, str] = {
    "_channel_binding": "owner ruling P9-D9: Telegram has no channel binding",
    "_channel_history": "Bot API has no history read",
}
# Differences the composition-root refactor removes (assessment D2, hand-copied platform wiring). None today.
DRIFT: Dict[str, str] = {}


def wired(handler: object) -> Set[str]:
    return {name for name, value in vars(handler).items() if value is not None}


def problems(slack: Set[str], telegram: Set[str], declared: Dict[str, str]) -> list:
    found = []
    for name in sorted((slack ^ telegram) - set(declared)):
        side, other = ("Slack", "Telegram") if name in slack else ("Telegram", "Slack")
        found.append(f"{name} is set on the {side} handler and None or missing on the {other} one: pass it to both "
                     f"platforms' ConversationHandler (a real platform difference goes in PLATFORM_ONLY with its reason)")
    for name in sorted(set(declared) - (slack ^ telegram)):
        found.append(f"{name} is declared as a platform difference but both handlers now agree on it: delete its entry")
    return found


@pytest.mark.slow
async def test_the_slack_and_the_telegram_handlers_hold_the_same_dependencies(prod_graph):
    app = prod_graph.app
    slack = wired(app.slack_adapter.conversation_handler)
    telegram = wired(app.telegram_adapter.conversation_handler)

    found = problems(slack, telegram, {**PLATFORM_ONLY, **DRIFT})

    assert not found, "\n".join(found)


def test_an_undeclared_difference_and_a_stale_declaration_are_reported():
    found = problems({"_a", "_b", "_c"}, {"_a", "_d"}, {"_b": "reason", "_e": "reason"})

    assert [p.split(" ")[0] for p in found] == ["_c", "_d", "_e"]
    assert "set on the Telegram handler" in found[1] and "delete its entry" in found[2]

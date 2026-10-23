"""A Telegram blueprint that cannot be built or registered degrades `main()`, it does not stop it (P9 T2, F1).

`main()` registers the Telegram adapter's blueprint in its own small `try`. With `get_blueprint` raising, the boot
(the T1 harness: the deploy's environment, fakes at the SDK boundaries only) must log the "continue without Telegram"
line, serve the pinned `url_map` minus exactly the `/telegram/*` rules, and still answer `/worker`.
"""
import pytest

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from tests.unit.test_main_boots_offline import MATRIX, URL_MAP, _boot

pytestmark = pytest.mark.slow

CONTINUE_LINE = "🤖 Bot will continue without Telegram support"


def _pinned_rules():
    return [line for line in URL_MAP.read_text().splitlines() if line and not line.startswith("#")]


def _is_telegram_rule(rule: str) -> bool:
    return rule.split(" ", 1)[1].startswith("/telegram/")


def test_the_pinned_url_map_has_telegram_rules_to_lose():
    assert [rule for rule in _pinned_rules() if _is_telegram_rule(rule)] == ["POST /telegram/webhook"]


async def test_a_telegram_blueprint_failure_costs_only_the_telegram_routes(monkeypatch, caplog):
    def broken(self):
        raise RuntimeError("telegram blueprint down")

    monkeypatch.setattr(TelegramWebhookAdapter, "get_blueprint", broken)
    boot = await _boot(monkeypatch, caplog)
    if boot.probe_error is not None:
        raise boot.probe_error

    messages = [record.getMessage() for record in boot.startup]
    assert CONTINUE_LINE in messages
    assert any(m.startswith("❌ Failed to initialize Telegram adapter: telegram blueprint down") for m in messages)

    expected = [rule for rule in _pinned_rules() if not _is_telegram_rule(rule)]
    assert boot.rules == expected

    row = boot.rows["fire_due_reminders"]
    assert (row.status, row.body) == (MATRIX["fire_due_reminders"].status, MATRIX["fire_due_reminders"].body)
    assert row.status == 200
    assert boot.shutdown_logged

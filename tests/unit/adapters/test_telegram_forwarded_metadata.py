"""A forwarded Telegram message is flagged in the turn's metadata, so Smart treats it as outside text
(EXTERNAL_TASK_QUEUE_RFC §5.3)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from src.adapters.telegram.webhook_adapter import _message_metadata


def _message(forward_origin):
    return SimpleNamespace(message_id=7, date=datetime(2026, 11, 23, tzinfo=timezone.utc), reply_to_message=None,
                           forward_origin=forward_origin, quote=None, external_reply=None)


def test_a_forwarded_message_is_flagged_and_an_ordinary_one_is_not():
    kwargs = dict(chat_id=1, telegram_user_id=2, update_id=3)
    assert _message_metadata(_message(object()), **kwargs)["forwarded"] is True
    assert "forwarded" not in _message_metadata(_message(None), **kwargs)

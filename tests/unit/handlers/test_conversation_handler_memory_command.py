"""ConversationHandler: the memory preview delivery and the `$memory save` command
(docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import DeliveryItem
from src.domain.exceptions import MemoryDraftNotFound
from src.domain.memory_fact import (
    MEMORY_PREVIEW_DELIVERY,
    MemoryFactDraft,
    ProposedFact,
    render_preview_file,
)
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales import uk as uk_locale

_USER_ID = "user-test"
_SESSION_ID = "user-test:C1"
FACT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}
DRAFT = MemoryFactDraft(facts=[ProposedFact(**FACT), ProposedFact(text="User has a daughter, Ana.", source="family.pdf")])


def _ui(message: UIMessage, **fmt) -> str:
    template = uk_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _context(text: str = "memory save ab12") -> MessageContext:
    return MessageContext(text=text, session_id=_SESSION_ID, user_id=_USER_ID, account_id="acc-test",
                          attachments=[], metadata={})


def _channel() -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.send_file = AsyncMock()
    ch.language = None
    return ch


def _handler(memory_fact_service=None):
    handler = ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(), file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        memory_fact_service=memory_fact_service,
    )
    return handler


def _service(count=2):
    s = MagicMock()
    s.confirm = AsyncMock(return_value=count)
    return s


def _sent(ch):
    return [c.args[0] for c in ch.send_message.await_args_list]


class TestPreviewDelivery:

    async def test_file_then_header_with_count_then_command(self):
        handler = _handler()
        ch = _channel()
        order = []
        ch.send_file.side_effect = lambda **kw: order.append("file")
        ch.send_message.side_effect = lambda text, thread: order.append(text)
        item = DeliveryItem(type=MEMORY_PREVIEW_DELIVERY, data={"facts": [FACT], "command": "$memory save ab12"})

        await handler._deliver_item(item, ch, "T1", _USER_ID)

        kw = ch.send_file.await_args.kwargs
        assert kw["content"] == render_preview_file(MemoryFactDraft(facts=[ProposedFact(**FACT)])).encode("utf-8")
        assert kw["filename"] == "memory-facts.txt"
        assert kw["title"] == _ui(UIMessage.MEMORY_PREVIEW_FILE_TITLE, count=1)
        assert kw["thread_id"] == "T1"
        assert order == ["file", _ui(UIMessage.MEMORY_PREVIEW_HEADER, count=1), "`$memory save ab12`"]
        assert all(c.args[1] == "T1" for c in ch.send_message.await_args_list)

    async def test_failed_file_sends_notice_and_never_the_command(self):
        handler = _handler()
        ch = _channel()
        ch.send_file.side_effect = RuntimeError("slack down")
        item = DeliveryItem(type=MEMORY_PREVIEW_DELIVERY, data={"facts": [FACT], "command": "$memory save ab12"})

        await handler._deliver_item(item, ch, None, _USER_ID)

        assert _sent(ch) == [_ui(UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED)]

    async def test_failed_header_sends_notice_and_never_the_command(self):
        handler = _handler()
        ch = _channel()
        ch.send_message.side_effect = [RuntimeError("slack down"), None]
        item = DeliveryItem(type=MEMORY_PREVIEW_DELIVERY, data={"facts": [FACT], "command": "$memory save ab12"})

        await handler._deliver_item(item, ch, None, _USER_ID)

        sent = _sent(ch)
        assert sent[-1] == _ui(UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED)
        assert not any("$memory save" in t for t in sent)


class TestMemorySave:

    async def test_save_confirms_with_the_paste_session_as_fallback(self):
        service = _service(2)
        handler = _handler(service)
        ch = _channel()

        await handler.handle_command("memory save ab12", _context(), ch)

        service.confirm.assert_awaited_once_with(_USER_ID, "ab12", fallback_session_id=_SESSION_ID)
        assert _sent(ch) == [_ui(UIMessage.MEMORY_SAVED, count=2)]

    async def test_unknown_code(self):
        service = _service()
        service.confirm.side_effect = MemoryDraftNotFound("none")
        handler = _handler(service)
        ch = _channel()

        await handler.handle_command("memory save ffff", _context(), ch)

        assert _sent(ch) == [_ui(UIMessage.MEMORY_DRAFT_NOT_FOUND, code="ffff")]

    async def test_any_other_error_says_unavailable_and_never_escapes(self):
        service = _service()
        service.confirm.side_effect = RuntimeError("firestore down")
        handler = _handler(service)
        ch = _channel()

        await handler.handle_command("memory save ab12", _context(), ch)

        assert _sent(ch) == [_ui(UIMessage.MEMORY_UNAVAILABLE)]

    async def test_bare_or_malformed_command_prints_usage(self):
        service = _service()
        handler = _handler(service)
        for command in ("memory", "memory save", "memory list", "memory save a b"):
            ch = _channel()
            await handler.handle_command(command, _context(command), ch)
            assert _sent(ch) == [_ui(UIMessage.MEMORY_USAGE)]
        service.confirm.assert_not_awaited()

    async def test_no_service_configured(self):
        handler = _handler(None)
        ch = _channel()

        await handler.handle_command("memory save ab12", _context(), ch)

        assert _sent(ch) == [_ui(UIMessage.MEMORY_UNAVAILABLE)]

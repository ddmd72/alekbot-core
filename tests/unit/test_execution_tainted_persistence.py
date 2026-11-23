"""The per-turn taint bit survives a save and a load (EXTERNAL_TASK_QUEUE_RFC §5.3, execution taint): the stored
model message carries it, an old record without it reads as clean, and the handler writes it from the response."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import Message, MessagePart
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler


def _store() -> FirestoreSessionStore:
    return FirestoreSessionStore(MagicMock())


def test_the_flag_round_trips_and_is_written_only_when_set():
    store = _store()
    history = [Message(role="model", parts=[MessagePart(text="a")], execution_tainted=True),
               Message(role="model", parts=[MessagePart(text="b")])]
    stored = store._serialize_history(history)
    assert stored[0]["execution_tainted"] is True and "execution_tainted" not in stored[1]
    assert [m.execution_tainted for m in store._deserialize_history(stored)] == [True, False]


def test_an_old_record_without_the_field_reads_as_clean():
    old = [{"role": "model", "parts": [{"text": "old"}], "created_at": 1.0}]
    assert _store()._deserialize_history(old)[0].execution_tainted is False


async def test_the_handler_saves_the_flag_on_the_model_message_only():
    handler = ConversationHandler(coordinator=MagicMock(), agent_factory=MagicMock(),
                                  global_config=ConsolidationSettings(threshold=50, batch_size=30))
    sessions = MagicMock()
    sessions.append_messages_batch = AsyncMock()

    await handler._save_history_with_retry(sessions, "usr-1:C1", [MessagePart(text="q")], "a", "a", "usr-1",
                                           execution_tainted=True)

    user, model = sessions.append_messages_batch.await_args.args[1]
    assert (user.execution_tainted, model.execution_tainted) == (False, True)

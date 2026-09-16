from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.consolidation import ConsolidationBatch
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.llm import Message, MessagePart
from src.services.overflow_routing_service import OverflowRoutingService

_SESSION_ID_ALEK = "user-1:C1"
_SESSION_ID_COMPANION = "slack:C1"


def _make_messages():
    return [Message(role="user", parts=[MessagePart(text="hola")], created_at=1000.0)]


@pytest.fixture
def channel_binding_service():
    return AsyncMock()


@pytest.fixture
def user_repo():
    r = MagicMock()
    profile = MagicMock()
    profile.account_id = "acc-1"
    r.get_user = AsyncMock(return_value=profile)
    return r


@pytest.fixture
def consolidation_queue():
    q = AsyncMock()
    q.enqueue_batch.return_value = "batch-1"
    return q


@pytest.fixture
def companion_extraction_queue():
    q = AsyncMock()
    q.enqueue_batch.return_value = "cbatch-1"
    return q


@pytest.fixture
def task_queue():
    return AsyncMock()


@pytest.fixture
def service(channel_binding_service, user_repo, consolidation_queue, companion_extraction_queue, task_queue):
    return OverflowRoutingService(
        channel_binding_service=channel_binding_service,
        user_repo=user_repo,
        consolidation_queue=consolidation_queue,
        companion_extraction_queue=companion_extraction_queue,
        task_queue=task_queue,
    )


async def test_no_binding_routes_to_alek(service, channel_binding_service, consolidation_queue, companion_extraction_queue, task_queue):
    channel_binding_service.get.return_value = None
    await service.route_overflow("user-1", _SESSION_ID_ALEK, _make_messages())
    consolidation_queue.enqueue_batch.assert_called_once()
    batch = consolidation_queue.enqueue_batch.call_args[0][0]
    assert isinstance(batch, ConsolidationBatch)
    assert batch.user_id == "user-1"
    assert batch.session_id == _SESSION_ID_ALEK
    task_queue.enqueue_consolidation_task.assert_called_once_with(user_id="user-1")
    companion_extraction_queue.enqueue_batch.assert_not_called()


async def test_binding_without_companion_config_routes_to_alek(service, channel_binding_service, consolidation_queue, companion_extraction_queue):
    channel_binding_service.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="domain_researcher", intent="domain_research",
        created_by="user-1", companion_config=None,
    )
    await service.route_overflow("user-1", _SESSION_ID_ALEK, _make_messages())
    consolidation_queue.enqueue_batch.assert_called_once()
    companion_extraction_queue.enqueue_batch.assert_not_called()


async def test_binding_with_companion_config_routes_to_companion(
    service, channel_binding_service, user_repo, consolidation_queue, companion_extraction_queue, task_queue,
):
    channel_binding_service.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="user-1",
        companion_config=CompanionConfig(window_threshold=100, batch_size=50),
    )
    await service.route_overflow("user-1", _SESSION_ID_COMPANION, _make_messages())

    companion_extraction_queue.enqueue_batch.assert_called_once()
    batch = companion_extraction_queue.enqueue_batch.call_args[0][0]
    assert isinstance(batch, CompanionExtractionBatch)
    assert batch.session_id == _SESSION_ID_COMPANION
    assert batch.account_id == "acc-1"
    assert batch.companion_type == "tutor"
    assert batch.created_by_user_id == "user-1"
    task_queue.enqueue_companion_consolidation_task.assert_called_once_with(session_id=_SESSION_ID_COMPANION)
    consolidation_queue.enqueue_batch.assert_not_called()

    channel_binding_service.get.assert_called_once_with("C1")


async def test_profile_with_none_account_id_falls_back_to_user_id(
    service, channel_binding_service, user_repo, companion_extraction_queue,
):
    """A UserProfile can exist with account_id still None (Optional[str] = None).
    CompanionExtractionBatch.account_id is a required str — before Important #3's
    fix this hit a pydantic ValidationError, silently dropping the batch."""
    profile = MagicMock()
    profile.account_id = None
    user_repo.get_user = AsyncMock(return_value=profile)
    channel_binding_service.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="user-1",
        companion_config=CompanionConfig(window_threshold=100, batch_size=50),
    )
    await service.route_overflow("user-1", _SESSION_ID_COMPANION, _make_messages())

    companion_extraction_queue.enqueue_batch.assert_called_once()
    batch = companion_extraction_queue.enqueue_batch.call_args[0][0]
    assert batch.account_id == "user-1"


async def test_companion_text_mode_is_consumed(service, channel_binding_service, companion_extraction_queue):
    from src.domain.companion_config import CompanionTextMode
    channel_binding_service.get.return_value = ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="user-1",
        companion_config=CompanionConfig(window_threshold=100, batch_size=50, text_mode=CompanionTextMode.FULL),
    )
    messages = [Message(role="model", parts=[MessagePart(text="short", full_text="the full verbose text")], created_at=1000.0)]
    await service.route_overflow("user-1", _SESSION_ID_COMPANION, messages)
    batch = companion_extraction_queue.enqueue_batch.call_args[0][0]
    assert batch.messages[0]["parts"][0]["text"] == "the full verbose text"


async def test_missing_consolidation_queue_logs_and_does_not_raise(channel_binding_service, user_repo, companion_extraction_queue, task_queue):
    service = OverflowRoutingService(
        channel_binding_service=channel_binding_service, user_repo=user_repo,
        consolidation_queue=None, companion_extraction_queue=companion_extraction_queue, task_queue=task_queue,
    )
    channel_binding_service.get.return_value = None
    await service.route_overflow("user-1", _SESSION_ID_ALEK, _make_messages())  # must not raise


async def test_exception_is_caught_and_logged_not_raised(service, channel_binding_service):
    channel_binding_service.get.side_effect = Exception("Firestore down")
    await service.route_overflow("user-1", _SESSION_ID_ALEK, _make_messages())  # must not raise


async def test_binding_lookup_failure_falls_open_to_alek_routing(
    service, channel_binding_service, consolidation_queue, companion_extraction_queue,
):
    """A binding-lookup failure must NOT drop the batch — it falls through to
    Alek's consolidation pipeline (the safe default), matching
    CompanionWindowResolver.resolve's identical lookup (Minor #8, final
    whole-branch review 2026-08-31). Before this fix the single outer
    try/except swallowed the whole route_overflow call, silently losing an
    Alek batch on a transient Firestore blip."""
    channel_binding_service.get.side_effect = Exception("Firestore down")

    await service.route_overflow("user-1", _SESSION_ID_ALEK, _make_messages())

    consolidation_queue.enqueue_batch.assert_called_once()
    companion_extraction_queue.enqueue_batch.assert_not_called()

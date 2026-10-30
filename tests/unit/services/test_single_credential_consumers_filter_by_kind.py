"""Single-credential consumers find their credential by the kind of their injected spec, never by integration
(MULTI_DRIVE_RFC §4.2, E25): `TASKS` has two kinds, so a Google Tasks grant must never feed To Do's renewal fan-out,
status or disconnect, and a Gmail search reads only the Gmail grant. Real credential store over the evaluating fake."""
from unittest.mock import AsyncMock

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.domain.task import TaskUserConfig
from src.services.email_search_service import EmailSearchService
from src.services.task_setup_service import TaskSetupService
from tests.support.oauth_credentials_store import CredentialsStore, creds

ALEX, CLARA = "user-alex", "user-clara"
REGISTRY = ConnectorRegistry()


class _Config:
    def __init__(self) -> None:
        self.saved = {}

    async def get_config(self, user_id):
        return TaskUserConfig()

    async def save_config(self, user_id, config):
        self.saved[user_id] = config


@pytest.fixture
async def store() -> CredentialsStore:
    s = CredentialsStore()
    await s.repo.save(creds(ALEX, "microsoft_todo"))
    await s.repo.save(creds(ALEX, "google_tasks"))
    await s.repo.save(creds(CLARA, "google_tasks"))
    return s


def _todo_setup(store) -> TaskSetupService:
    index = AsyncMock()
    return TaskSetupService(lifecycle=AsyncMock(), task_config=_Config(), tasks_provider=AsyncMock(),
                            oauth_credentials=store.repo, task_search_index=index, task_queue=AsyncMock(),
                            notification_url_base="https://example.com",
                            connector=REGISTRY.get("microsoft_todo"))


async def test_the_to_do_fan_out_lists_only_to_do_users(store):
    assert await _todo_setup(store).list_microsoft_users() == [ALEX]


async def test_the_to_do_status_reads_only_the_to_do_grant(store):
    svc = _todo_setup(store)
    assert (await svc.get_status(ALEX))["connected"] is True
    assert (await svc.get_status(CLARA))["connected"] is False


async def test_the_to_do_disconnect_deletes_only_the_to_do_grant(store):
    await _todo_setup(store).disconnect(ALEX)
    assert [c.kind for c in await store.repo.list_for_user(ALEX)] == ["google_tasks"]
    assert len(await store.repo.list_for_user(CLARA)) == 1


async def test_a_gmail_read_never_takes_another_kinds_grant():
    s = CredentialsStore()
    await s.repo.save(creds(ALEX, "google_tasks", access_token="at-tasks"))
    gmail = AsyncMock()
    svc = EmailSearchService(indexed_email_repo=AsyncMock(), oauth_credentials=s.repo, gmail_provider=gmail,
                             embedding_service=AsyncMock(), connector=REGISTRY.get("gmail"))
    assert "not connected" in await svc.get_details("e-1", ALEX)
    gmail.batch_get_full_content.assert_not_awaited()

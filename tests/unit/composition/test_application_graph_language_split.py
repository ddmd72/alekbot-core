"""The language read/write split in the production graph: one `LanguageResolver` is the language every reader gets at
construction (the notification service with its localization, the agent factory), and the one writer is built over that
resolver, the notification service and the agent factory's refresh. Nothing is attached after construction."""
import pytest

from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_readers_share_the_one_resolver(prod_graph: ProdGraph):
    app = prod_graph.app

    assert type(app.language_service) is LanguageResolver
    assert app.notification_service._language is app.language_service
    assert app.notification_service._localization is app.localization
    assert app.agent_factory.language_service is app.language_service


async def test_the_writer_is_built_over_the_resolver_the_notifier_and_the_agent_refresh(prod_graph: ProdGraph):
    app = prod_graph.app
    writer = app.language_preferences

    assert type(writer) is LanguagePreferenceService
    assert writer._resolver is app.language_service
    assert writer._notification_service is app.notification_service
    assert writer._ensure_agents == app.agent_factory.ensure_agents_for_user
    assert writer._user_repo is app.user_repo
    assert writer._profile_repo is app.container.profile_repo
    assert writer._prompt_builder is app.container.assembly_service

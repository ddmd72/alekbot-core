"""AgentServices: the container's shared services for UserAgentFactory as one typed bundle (assessment D5). Each field
carries the container's own object, and the production factory holds those same objects."""
import dataclasses

import pytest

from src.composition.agent_services import AgentServices
from tests.support.agent_services_kit import user_agent_factory_kwargs
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)
from tests.unit.composition.test_service_container_wiring import container, fake_config  # noqa: F401  (fixtures)

# field -> the container attribute it carries; a field added to AgentServices without a line here fails the first test.
_CONTAINER_ATTRIBUTE = {
    "session_store": "session_store",
    "embedding_service": "embedding_service",
    "repository": "repository",
    "config_service": "config_service",
    "biographical_context_service": "biographical_context_service",
    "context_builder": "context_builder",
    "assembly_service": "assembly_service",
    "fact_write_service": "fact_write_service",
    "email_search_service": "email_search_service",
    "email_card_search": "email_card_search",
    "indexed_email_repo": "indexed_email_repo",
    "confirmed_people": "confirmed_people",
    "tasks_provider": "ms_todo_adapter",
    "task_indexing": "task_indexing",
    "notes_provider": "notes_adapter",
    "recurrence": "recurrence_adapter",
    "file_conversion_service": "file_conversion_service",
    "file_storage": "file_storage",
    "user_drive_service": "user_drive_service",
    "drive_search_service": "drive_search_service",
    "prompt_content_store": "prompt_content_store",
    "skill_service": "skill_service",
    "memory_fact_service": "memory_fact_service",
    "lifecycle_counters": "fact_lifecycle_counters",
    "workspace_service": "workspace_service",
    "drive_note_service": "drive_note_service",
    "drive_notes_enabled": "drive_notes_enabled",
}
# Not read by UserAgentFactory yet, so not stored on it (the bio-cycle slice makes it read this one).
_NOT_STORED_BY_THE_FACTORY = {"biographical_context_service"}


def test_each_field_carries_the_containers_own_object(container):  # noqa: F811
    services = container.agent_services()

    names = [f.name for f in dataclasses.fields(AgentServices)]
    assert set(names) == set(_CONTAINER_ATTRIBUTE) | {"fact_management_adapter_factory"}
    for name, attribute in _CONTAINER_ATTRIBUTE.items():
        assert getattr(services, name) is getattr(container, attribute), name
    assert services.fact_management_adapter_factory == container.create_fact_management_adapter


def test_the_bundle_is_frozen(container):  # noqa: F811
    services = container.agent_services()

    with pytest.raises(dataclasses.FrozenInstanceError):
        services.session_store = None  # type: ignore[misc]


@pytest.mark.slow
def test_the_production_factory_holds_the_containers_objects(prod_graph: ProdGraph):  # noqa: F811
    app = prod_graph.app
    services = app.container.agent_services()

    for f in dataclasses.fields(AgentServices):
        if f.name in _NOT_STORED_BY_THE_FACTORY:
            assert not hasattr(app.agent_factory, f.name), f.name
        elif f.name == "fact_management_adapter_factory":
            assert app.agent_factory.fact_management_adapter_factory == services.fact_management_adapter_factory
        else:
            assert getattr(app.agent_factory, f.name) is getattr(services, f.name), f.name


def test_the_test_kit_moves_service_keywords_into_the_bundle_and_keeps_the_rest():
    session_store, coordinator = object(), object()

    kwargs = user_agent_factory_kwargs(session_store=session_store, coordinator=coordinator)

    assert set(kwargs) == {"coordinator", "services"} and kwargs["coordinator"] is coordinator
    assert kwargs["services"].session_store is session_store
    assert kwargs["services"].drive_notes_enabled is True and kwargs["services"].biographical_context_service is None

"""`build_session_routing`: the overflow callback routes over the same queues and channel bindings the bundle hands on, and
the threshold resolver reads the same bindings."""
import dataclasses

import pytest

from src.composition.session_routing_setup import build_session_routing
from src.config.environment import EnvironmentConfig
from src.services.companion_window_resolver import CompanionWindowResolver
from src.services.overflow_routing_service import OverflowRoutingService
from tests.support.firestore_eval import EvalDb


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    return EnvironmentConfig()


@pytest.mark.parametrize("task_queue", [object(), None])
def test_the_callbacks_run_over_the_bundles_queues_and_bindings(env, task_queue):
    users = object()

    routing = build_session_routing(EvalDb(), env, user_repo=users, task_queue=task_queue)  # type: ignore[arg-type]

    router = routing.overflow_callback.__self__
    assert isinstance(router, OverflowRoutingService) and routing.overflow_callback == router.route_overflow
    assert router._channel_binding is routing.channel_binding_service and router._user_repo is users
    assert router._consolidation_queue is routing.consolidation_queue
    assert router._companion_extraction_queue is routing.companion_extraction_queue
    assert router._task_queue is task_queue
    assert routing.channel_binding_service._port is routing.channel_binding_adapter
    resolver = routing.threshold_resolver.__self__
    assert isinstance(resolver, CompanionWindowResolver) and resolver._bindings is routing.channel_binding_service


def test_the_bundle_is_frozen(env):
    routing = build_session_routing(EvalDb(), env, user_repo=object(), task_queue=None)  # type: ignore[arg-type]

    with pytest.raises(dataclasses.FrozenInstanceError):
        routing.consolidation_queue = None  # type: ignore[misc]

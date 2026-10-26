"""Facts S4 · T10a: the production graph builds the memory review and the owner's answers over the container's stores, one
lease shared with consolidation, one router, one set of question texts and one owner-zone resolver (plan §5 T10a, §3.16,
§3.20).

Each check reads the attribute that holds the dependency in the object `/worker` uses.
"""
import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.adapters.firestore_fact_review_store import FirestoreFactReviewStore
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.fact_review_task_keys import FACT_REVIEW_TASK, review_payload
from src.infrastructure.agent_config import CONSOLIDATION
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from src.services import fact_answer_service as answer_module
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


def _review(app) -> FactReviewService:
    review = app.worker_handler._fact_review
    assert isinstance(review, FactReviewService)
    return review


async def test_the_review_holds_the_containers_store_repository_and_counters(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)

    assert isinstance(app.container.fact_review_store, FirestoreFactReviewStore)
    assert review._review_store is app.container.fact_review_store
    assert review._repo is app.container.repository
    assert review._counters is app.container.fact_lifecycle_counters


async def test_the_review_holds_the_graphs_own_objects_for_its_other_dependencies(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)

    assert review._coordinator is app.coordinator
    assert review._agent_factory is app.agent_factory
    assert review._user_repo is app.user_repo
    assert review._account_repo is app.account_repo
    assert review._notification_state is app.notification_service._state_repo
    assert review._notifications is app.notification_service
    assert review._session_store is app.container.session_store
    assert review._task_queue is app.agent_task_queue and review._task_queue is not None
    assert review._alert_sink is app.alert_sink and review._alert_sink is not None
    # The ledger counts one FirestoreChannelBindingAdapter in the graph, so the type proves it is the graph's one.
    assert isinstance(review._channel_bindings, FirestoreChannelBindingAdapter)


async def test_the_review_and_consolidation_hold_the_same_lease(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)

    assert isinstance(review._lease, FirestoreMemoryWriteLease)
    assert review._lease is app.worker_handler._consolidation._lease


async def test_the_review_answer_and_consolidation_share_one_owner_zone_resolver(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)
    zones = review._owner_zones

    assert isinstance(zones, OwnerTimezoneResolver)
    assert review._answer_service._owner_zones is zones
    assert app.worker_handler._consolidation._owner_zones is zones
    assert zones._account_repo is app.account_repo and zones._user_repo is app.user_repo


async def test_the_review_and_the_answer_share_one_router_and_one_set_of_question_texts(prod_graph: ProdGraph):
    review = _review(prod_graph.app)

    assert isinstance(review._interactive, InteractiveMessageRouter)
    assert review._answer_service._interactive is review._interactive
    assert isinstance(review._texts, FactQuestionTexts)
    assert review._answer_service._texts is review._texts


async def test_the_router_posts_through_the_slack_apps_client_and_the_notification_channels(prod_graph: ProdGraph):
    app = prod_graph.app
    adapters = _review(app)._interactive._adapters

    assert set(adapters) == {"slack", "telegram"}
    assert isinstance(adapters["slack"], SlackInteractiveMessageAdapter)
    assert adapters["slack"]._client is app.slack_adapter.app.client
    assert isinstance(adapters["telegram"], PlainTextInteractiveMessageAdapter)
    assert adapters["telegram"]._channels is app.notification_service._channel_factory


async def test_the_question_texts_read_the_graphs_language_localization_store_and_counters(prod_graph: ProdGraph):
    app = prod_graph.app
    texts = _review(app)._texts

    assert texts._language is app.language_service
    assert texts._localization is app.localization._port
    assert texts._review_store is app.container.fact_review_store
    assert texts._counters is app.container.fact_lifecycle_counters
    assert texts._system_default is app.language_service._system_default


async def test_the_answer_services_whole_constructor_is_the_graphs_objects(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)
    answer = review._answer_service

    assert isinstance(answer, FactAnswerService)
    expected = {
        "_repo": app.container.repository,
        "_review_store": app.container.fact_review_store,
        "_counters": app.container.fact_lifecycle_counters,
        "_task_queue": app.agent_task_queue,
        "_session_store": app.container.session_store,
        "_texts": review._texts,
        "_interactive": review._interactive,
        "_owner_zones": review._owner_zones,
        "_clock": answer_module._utcnow,
    }
    held = vars(answer)
    assert set(held) == set(expected) | {"_fact_updates"}
    for name, value in expected.items():
        assert held[name] is value, name

    updates = answer._fact_updates
    assert isinstance(updates, FactManagementAdapter)
    assert updates._repo is app.container.repository
    assert updates._embedding is app.container.embedding_service
    assert updates._fact_write_service is app.container.fact_write_service
    assert isinstance(updates._search_enrichment, SearchEnrichmentService)
    assert updates._search_enrichment._repo is app.container.repository


async def test_the_review_run_timeout_is_the_consolidation_runs(prod_graph: ProdGraph):
    app = prod_graph.app
    review = _review(app)

    assert review._run_timeout_s == CONSOLIDATION.timeout_ms / 1000 == 900.0
    assert review._run_timeout_s == app.worker_handler._consolidation._run_timeout_s


async def test_the_graph_answers_a_review_of_a_user_who_is_not_the_owner(prod_graph: ProdGraph):
    # The seeded account has no owner, so the run stops at the owner check; the answer is the one `/worker` gives.
    for mode in ("daily", "next"):
        result = await prod_graph.app.worker_handler.handle(
            {"task_type": FACT_REVIEW_TASK, **review_payload("u1", "a1", mode)})

        assert result == ({"status": "not_owner"}, 200)

"""The background memory work over the fact store: consolidation (Facts S2), the fact janitor (FACT_LIFECYCLE_RFC D4), the
memory review with the owner's answers (Facts S4) and the Cabinet's fact writes.

Shared here, one instance each: the owner-zone resolver (the consolidation run, the review and the answers), the
memory-write lease (a consolidation batch and a review of the same account never write at once) and the profile step
(fact profile RFC §4.4), run inside the lease by both the consolidation and the review.
"""
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ..adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from ..adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from ..adapters.slack.interactive_message_adapter import PLATFORM as SLACK_PLATFORM
from ..adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from ..config.environment import EnvironmentConfig
from ..domain.fact_lifecycle_sweep import sweep_dry_run
from ..domain.fact_review_question import FACT_QUESTION_KIND
from ..domain.settings import SearchConfig
from ..infrastructure.agent_config import CONSOLIDATION as CONSOLIDATION_CFG
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.interactive_message_router import InteractiveMessageRouter
from ..services.cabinet_fact_service import CabinetFactService
from ..services.consolidation_service import ConsolidationService
from ..services.fact_answer_service import FactAnswerService
from ..services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from ..services.fact_question_texts import FactQuestionTexts
from ..services.fact_review_service import FactReviewService
from ..services.interactive_choice_service import InteractiveChoiceService
from ..services.owner_timezone_resolver import OwnerTimezoneResolver
from ..services.profile_question_answers import ProfileQuestionAnswers
from ..services.search_enrichment_service import SearchEnrichmentService
from ..utils.logger import logger
from .fact_profile_setup import build_fact_profile_service, max_per_run as profile_max_per_run
from .identity_setup import Identity
from .language_setup import Languages
from .memory_search_setup import MemorySearch
from .notification_setup import Notifications
from .operations_setup import Operations
from .session_routing_setup import SessionRouting


@dataclass(frozen=True)
class Memory:
    """`fact_answers`, `interactive_choice` and `fact_review` are None without a task queue (the review also needs the
    ops alert sink); `/worker` then answers their tasks with 501."""

    consolidation_service: ConsolidationService
    fact_lifecycle_sweep: FactLifecycleSweepService
    fact_answers: Optional[FactAnswerService]
    interactive_choice: Optional[InteractiveChoiceService]
    fact_review: Optional[FactReviewService]
    cabinet_facts: CabinetFactService


def build_memory(db_client: Any, env_config: EnvironmentConfig, *, container: Any, coordinator: AgentCoordinator,
                 agent_factory: Any, identity: Identity, ops: Operations, routing: SessionRouting,
                 memory_search: MemorySearch, notifications: Notifications, languages: Languages,
                 cabinet_memory_url: str) -> Memory:
    """The services over the container's fact store, built after the agent factory (consolidation and the review run
    agents). Two optional knobs are read here with os.getenv: FACT_PROFILER_MAX_PER_RUN (the profile step's cap) and
    FACT_LIFECYCLE_SWEEP_DRY_RUN (the janitor is log-only unless it is an explicit off value)."""
    owner_zones = OwnerTimezoneResolver(account_repo=identity.account_repo, user_repo=identity.user_repo)
    lease = FirestoreMemoryWriteLease(db_client, env_config)
    profile_step = build_fact_profile_service(
        container, max_per_run=profile_max_per_run(os.getenv("FACT_PROFILER_MAX_PER_RUN")),
        person_cards=memory_search.person_cards, atlas=memory_search.place_atlas, quota=ops.quota_service)
    consolidation = ConsolidationService(
        queue=routing.consolidation_queue,
        coordinator=coordinator,
        agent_factory=agent_factory,
        lease=lease,
        run_timeout_s=CONSOLIDATION_CFG.timeout_ms / 1000,
        task_queue=ops.task_queue,
        indexed_email_repo=container.indexed_email_repo,
        user_repo=identity.user_repo,
        owner_zones=owner_zones,
        profile_step=profile_step,
    )
    dry_run = sweep_dry_run(os.getenv("FACT_LIFECYCLE_SWEEP_DRY_RUN"))
    logger.info("fact lifecycle sweep: log-only" if dry_run else "fact lifecycle sweep: writes")
    sweep = FactLifecycleSweepService(
        repo=container.repository,
        counters=container.fact_lifecycle_counters,
        alert_sink=ops.alert_sink,
        dry_run=dry_run,
        review_store=container.fact_review_store,
        task_queue=ops.task_queue,
        owner_zones=owner_zones,
        account_repo=identity.account_repo,
        user_repo=identity.user_repo,
    )
    answers, choice, review = _build_fact_review(
        container=container, coordinator=coordinator, agent_factory=agent_factory, identity=identity, ops=ops,
        routing=routing, memory_search=memory_search, notifications=notifications, languages=languages,
        owner_zones=owner_zones, lease=lease, profile_step=profile_step, cabinet_memory_url=cabinet_memory_url)
    return Memory(consolidation_service=consolidation, fact_lifecycle_sweep=sweep, fact_answers=answers,
                  interactive_choice=choice, fact_review=review,
                  # The Cabinet's fact writes (the owner's delete), over the same fact repository the agents use.
                  cabinet_facts=CabinetFactService(container.repository))


def _build_fact_review(*, container: Any, coordinator: AgentCoordinator, agent_factory: Any, identity: Identity,
                       ops: Operations, routing: SessionRouting, memory_search: MemorySearch,
                       notifications: Notifications, languages: Languages, owner_zones: OwnerTimezoneResolver,
                       lease: FirestoreMemoryWriteLease, profile_step: Any, cabinet_memory_url: str) -> Tuple[Optional[FactAnswerService], Optional[InteractiveChoiceService],
                                                         Optional[FactReviewService]]:
    """The memory review, the owner's answers and the click handler. Interactive messages (Facts S4 plan §3.20) are
    buttons on Slack and numbered text on Telegram, picked by platform."""
    queue, sessions = ops.task_queue, container.session_store
    interactive = InteractiveMessageRouter({
        SLACK_PLATFORM: SlackInteractiveMessageAdapter(notifications.slack_app.client),
        "telegram": PlainTextInteractiveMessageAdapter(notifications.channel_factory),
    })
    texts = FactQuestionTexts(
        language=languages.resolver,
        localization=languages.localization_port,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        system_default=languages.system_default,
    )
    # One adapter for the answers' owner writes and the review's history fallbacks and folds. Its writes go through the
    # typed update path only, which never searches: the search service is its constructor argument, at the defaults.
    search = SearchConfig()
    fact_updates = container.create_fact_management_adapter(SearchEnrichmentService(
        repository=container.repository,
        embedding_service=container.embedding_service,
        keyword_limit=search.DEFAULT_KEYWORD_LIMIT,
        phrase_one_limit=search.DEFAULT_PHRASE_ONE_LIMIT,
        phrase_two_limit=search.DEFAULT_PHRASE_TWO_LIMIT,
        total_limit=search.DEFAULT_SEMANTIC_SEARCH_LIMIT,
    )) if queue else None
    # Profile questions (fact profile RFC §4.7): a place reply reads the atlas, a person reply the confirmed list.
    profile_answers = ProfileQuestionAnswers(
        container.profile_answers, atlas=memory_search.place_atlas, confirmed_people=container.confirmed_people)
    answers = FactAnswerService(
        fact_updates=fact_updates,
        repo=container.repository,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        task_queue=queue,
        session_store=sessions,
        texts=texts,
        interactive=interactive,
        owner_zones=owner_zones,
        profile_answers=profile_answers,
    ) if queue else None
    # The Slack interactivity route queues an owner's click; the worker's `interactive_choice` task runs it.
    choice = InteractiveChoiceService(
        iam=identity.iam_service,
        handlers={FACT_QUESTION_KIND: answers},
        interactive=interactive,
    ) if answers is not None else None
    # Needs its answer path (and so the queue, for the deferral and the next question) and its ops sink.
    review = FactReviewService(
        repo=container.repository,
        review_store=container.fact_review_store,
        counters=container.fact_lifecycle_counters,
        lease=lease,
        coordinator=coordinator,
        agent_factory=agent_factory,
        user_repo=identity.user_repo,
        account_repo=identity.account_repo,
        texts=texts,
        owner_zones=owner_zones,
        notification_state=notifications.state_repo,
        channel_bindings=routing.channel_binding_adapter,
        notifications=notifications.service,
        session_store=sessions,
        task_queue=queue,
        answer_service=answers,
        interactive=interactive,
        alert_sink=ops.alert_sink,
        run_timeout_s=CONSOLIDATION_CFG.timeout_ms / 1000,
        fact_updates=fact_updates,
        cabinet_url=cabinet_memory_url,
        profile_step=profile_step,
        person_cards=memory_search.person_cards,
    ) if (answers is not None and fact_updates is not None and queue is not None
          and ops.alert_sink is not None) else None
    return answers, choice, review

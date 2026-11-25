"""The memory inbox (MCP_MEMORY_SUBMISSIONS_RFC): its store and the service the MCP tool and the Cabinet call.

Built by `build_memory` after the owner-zone resolver, the memory-write lease and the profile step it shares. The bundle
carries what the three `/worker` runners (`memory_inbox_triage`, `memory_inbox_final`, `memory_inbox_sweep`, registered
by `build_worker_tasks`) need beside the store: the two passes built here over the coordinator, the shared lease, the
owner-zone resolver, the profile step run inside the lease after a write, the language resolver and the run timeout.
Without a task queue there is no inbox (None): a confirmation could not set its final pass, and the MCP tool, the
Cabinet routes and the three task types then answer as unavailable.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from ..adapters.firestore_memory_inbox_repository import FirestoreMemoryInboxRepository
from ..config.environment import EnvironmentConfig
from ..domain.memory_inbox_runs import FinalPass, TriagePass
from ..infrastructure.memory_inbox_triage_scope import MemoryInboxTriageScope
from ..ports.alert_sink import AlertSinkPort
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..ports.task_queue import TaskQueue
from ..services.memory_inbox_final_pass import MemoryInboxFinalPass
from ..services.memory_inbox_service import MemoryInboxService
from ..services.memory_inbox_triage_pass import MemoryInboxTriagePass
from ..services.owner_timezone_resolver import OwnerTimezoneResolver
from ..services.language_resolver import LanguageResolver


@dataclass(frozen=True)
class MemoryInbox:
    """`profile_step` is the fact profile step (`run(account_id, deadline)`) the final runner runs inside the lease after
    a pass that wrote facts; `run_timeout_s` is the lease's length."""

    store: MemoryInboxRepository
    service: MemoryInboxService
    triage_pass: TriagePass
    final_pass: FinalPass
    lease: MemoryWriteLeasePort
    owner_zones: OwnerTimezoneResolver
    profile_step: Any
    languages: LanguageResolver
    task_queue: TaskQueue
    run_timeout_s: float


def build_memory_inbox(db_client: Any, env_config: EnvironmentConfig, *, task_queue: Optional[TaskQueue],
                       owner_zones: OwnerTimezoneResolver, coordinator: Any,
                       ensure_agents: Callable[[str], Awaitable[Any]], facts: Any, alert_sink: Optional[AlertSinkPort],
                       lease: MemoryWriteLeasePort, profile_step: Any, languages: LanguageResolver,
                       run_timeout_s: float) -> Optional[MemoryInbox]:
    """The inbox's store, service and passes over the main Firestore client; None without a task queue. `facts` is the
    fact repository the triage scope validates a model-sent fact id against."""
    if task_queue is None:
        return None
    store = FirestoreMemoryInboxRepository(db_client, env_config)

    def new_scope(submission_ids: Sequence[str], account_id: str) -> MemoryInboxTriageScope:
        return MemoryInboxTriageScope(submission_ids, account_id, facts)

    return MemoryInbox(
        store=store,
        service=MemoryInboxService(store=store, task_queue=task_queue, owner_zones=owner_zones),
        triage_pass=MemoryInboxTriagePass(store=store, coordinator=coordinator, ensure_agents=ensure_agents,
                                          new_scope=new_scope),
        final_pass=MemoryInboxFinalPass(store=store, coordinator=coordinator, ensure_agents=ensure_agents,
                                        alert_sink=alert_sink),
        lease=lease, owner_zones=owner_zones, profile_step=profile_step, languages=languages, task_queue=task_queue,
        run_timeout_s=run_timeout_s)

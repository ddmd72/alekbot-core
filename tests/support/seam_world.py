"""The seam world (P13 card A): every service between an entry path and the agent is the production class, wired as
`build_application_graph` wires it, on one `EvalDb`; the agents are scripted stand-ins registered in the real coordinator.

Real: `AgentCoordinator` (with the real `AgentRegistry` of `ALL_DESCRIPTORS` and the raw `GcpTaskQueue`, as
`application_graph.py:221-223,339-347`), `UserNotificationService` (`:447-453`, with the session store), the platform media
ports registered after construction (`:929`, `:995`), `DocumentDeliveryService` (`:474-477`) over the real `GcsMediaAdapter`
and `FileLinkService`, `AgentWorkerHandler` (`:483-500`), `TaskDispatchService` (`:674`), `RemindersService` (`:675-682`),
`WorkerHandler` (`:818-858`), `FirestoreSessionStore` (`service_container.py:334-341`), `FirestoreNotificationStateAdapter`,
`FirestoreAgentNoteAdapter`, `FirestoreTaskClaimStore`; `ConversationHandler` on request (`chat_handler`).

Scripted (the boundaries): the agents (`StandInAgent`, no LLM, no network), the platform channels (`SeamChannels`), the
platform media uploads (`RecordingMedia`), the Cloud Tasks SDK client (`FailingTasksClient`), GCS (`FakeGcs`), the user
repository (`UserDirectory`) and the agent factory (`SeamFactory`).

Declared wiring deltas from production: the coordinator gets no `file_ref_resolver`, `alert_sink` or `agent_factory`
(the stand-ins are registered up front); `AgentWorkerHandler` gets no `short_link_service` and no
`deep_research_drive_copy`; `WorkerHandler` gets only the keywords `SeamWorld.__init__` passes (plus the caller's
`worker_overrides`); the platform channel factory, the media ports and the Cloud Tasks SDK client are recording fakes;
the session store has no overflow/threshold callbacks and a 50/30 window (live: 70/50); the queue has no OIDC account;
`chat_handler` passes no file service, consolidation queue, fallback, long-turn, skill or memory-fact service (`**kw`).

A stand-in that runs out of scripted steps records the message in `unscripted` and raises `RuntimeError`, which the
coordinator turns into `AgentResponse.failure` (`agent_coordinator.py:194-210`): a test that goes through a service sees
only a failed turn, so every pin (and every control whose pass could come from an exhausted script) starts with
`require(not world.unscripted(), ...)`.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.adapters import firestore_agent_note_adapter
from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentMessage, AgentResponse, DeliveryItem
from src.domain.agent_note import NoteCreate
from src.domain.messaging import SmartResponse
from src.domain.notification_kind import NotificationKind
from src.domain.settings import ConsolidationSettings
from src.domain.task_complexity import TaskComplexity
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.conversation_handler import ConversationHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.notification_sla import NOTIFICATION_SLA, dispatch_deadline_s
from src.ports.platform_media_port import PlatformMediaPort
from src.services.document_delivery_service import DocumentDeliveryService
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.services.reminders_service import RemindersService
from src.services.task_dispatch_service import TaskDispatchService
from src.services.user_notification_service import UserNotificationService
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.gcs_fake import FakeGcs
from tests.support.recording_task_queue import SERVICE_URL
from tests.support.redelivery_world import (
    ACCOUNT, T0, USER, Answer, Post, PostingChannel, PostingChannels, PreconditionFailed, UserDirectory, deliver,
    make_failing_queue, require,
)

__all__ = [
    "ACCOUNT", "BUCKET", "OTHER_SLACK", "PRIMARY", "SECRET", "T0", "TELEGRAM", "USER",
    "Delegation", "PreconditionFailed", "RecordingMedia", "SeamChannel", "SeamChannels", "SeamFactory", "SeamWorld",
    "StandInAgent", "Step", "TaskRequest", "Upload", "docx", "fails", "page", "pdf", "require", "says",
]

PRIMARY = ("slack", "C1")
OTHER_SLACK = ("slack", "C2")
TELEGRAM = ("telegram", "7001")
BUCKET = "media-seam"
# HS256 signing secret of the capability links (FileAccessTokenService wants 32 chars or more). Test-only.
SECRET = "seam-world-test-secret-0123456789abcdef"
# The session store's window and the chat handler's consolidation settings (the `is_production` values of
# config/settings.py); one value for both, so a test that fills a session meets the handler's own numbers.
SEAM_CONSOLIDATION = ConsolidationSettings(threshold=50, batch_size=30)


# ----------------------------------------------------------------------------- channels and media

class SeamChannel(PostingChannel):
    """`PostingChannel` plus the two methods the document paths use. Every post goes through the factory's `_post`, so
    `fail_post` counts these too.

    `send_document_link` (`notify_document_link`) records `Post(kind="doc", value=(label, url))`; `send_message` (the
    "📎" placeholder of `notify_file_bytes` on Slack) records `Post(kind="message", value=text)` and then, as Slack's
    `chat.postMessage` answers a DM opened by user id, turns a `U…` channel id into the `D…` one (`U1` → `D1`), which
    `notify_file_bytes` reads back for the upload (`user_notification_service.py:629-632`)."""

    async def send_document_link(self, url: str, label: str) -> None:
        self._factory._post(Post(self.platform, self.channel_id, "doc", (label, url)))

    async def send_message(self, text: str, thread_id: Optional[str] = None) -> None:
        self._factory._post(Post(self.platform, self.channel_id, "message", text))
        if self.platform == "slack" and self.channel_id.startswith("U"):
            self.channel_id = "D" + self.channel_id[1:]


class SeamChannels(PostingChannels):
    """`PostingChannels` whose channels are `SeamChannel`s (same keying and `unconfigured` rule)."""

    def create(self, platform: str, channel_id: str) -> Optional[SeamChannel]:  # type: ignore[override]
        if platform in self.unconfigured:
            return None
        channel = self.channels.setdefault((platform, channel_id), SeamChannel(self, platform, channel_id))
        return channel  # type: ignore[return-value]


@dataclass(frozen=True)
class Upload:
    """One media upload a platform port received; `filename` is None for an image."""
    platform: str
    channel_id: str
    filename: Optional[str]
    title: str
    size: int


class RecordingMedia(PlatformMediaPort):
    """The platform media boundary of one platform: records every upload in `uploads`."""

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.uploads: List[Upload] = []

    async def upload_image(self, image_bytes: bytes, alt_text: str, channel_id: str) -> None:
        self.uploads.append(Upload(self.platform, channel_id, None, alt_text, len(image_bytes)))

    async def upload_file(self, file_bytes: bytes, filename: str, title: str, channel_id: str) -> None:
        self.uploads.append(Upload(self.platform, channel_id, filename, title, len(file_bytes)))


# ----------------------------------------------------------------------------- the scripted agents

Respond = Callable[[AgentMessage], AgentResponse]


@dataclass(frozen=True)
class Delegation:
    """A `delegate_to_specialist` call a stand-in makes before it answers."""
    intent: str
    query: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    """One scripted call: what the stand-in answers (`respond(message)`), after the delegation it makes first."""
    respond: Respond
    delegate: Optional[Delegation] = None


def says(text: str, summary: Optional[str] = None) -> Respond:
    """A successful answer `SmartResponse(text)`; `metadata["response_summary"] = summary` when given (what
    `notify` stores as the model turn's text, `user_notification_service.py:445-446`)."""
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient,
                                     result=SmartResponse(text=text),
                                     metadata={"response_summary": summary} if summary is not None else None)
    return respond


def fails(error: str = "generator down") -> Respond:
    """A failed turn (`AgentResponse.failure`)."""
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(task_id=message.task_id, agent_id=message.recipient, error=error)
    return respond


def page(filename: str, label: str, content_type: str = "text/html; charset=utf-8") -> Respond:
    """A generated document, the item built as `html_page_generator_agent.py:176-189` builds it; `storage_class` is the
    agent's own read of the run context (`html_page_generator_agent.py:187`). The image generator delivers one item of
    this type too (`agent_worker_handler.py:140-144`); the PDF generator delivers two (`pdf`)."""
    def respond(message: AgentMessage) -> AgentResponse:
        content = b"<html><head></head><body>" + label.encode("utf-8") + b"</body></html>"
        return AgentResponse.success(
            task_id=message.task_id, agent_id=message.recipient, result="html_page_generated",
            delivery_items=[DeliveryItem(type="document", data={
                "content_b64": base64.b64encode(content).decode("ascii"),
                "filename": filename,
                "content_type": content_type,
                "label": label,
                "file_upload": False,
                "storage_class": message.context.get("storage_class", "document"),
            })])
    return respond


def pdf(base_filename: str, display_name: str) -> Respond:
    """A generated PDF, the two `document` items `pdf_generator_agent.py:228-249` builds: the HTML preview
    (`file_upload` False) and the PDF itself (`file_upload` True, so it is also uploaded as a file,
    `agent_worker_handler.py:283-292`). Neither carries a `storage_class`."""
    def respond(message: AgentMessage) -> AgentResponse:
        html = b"<html><head></head><body>" + display_name.encode("utf-8") + b"</body></html>"
        return AgentResponse.success(
            task_id=message.task_id, agent_id=message.recipient, result="pdf_generated",
            delivery_items=[
                DeliveryItem(type="document", data={
                    "content_b64": base64.b64encode(html).decode("ascii"),
                    "filename": f"{base_filename}.html",
                    "content_type": "text/html; charset=utf-8",
                    "label": f"{display_name}.html",
                    "file_upload": False,
                }),
                DeliveryItem(type="document", data={
                    "content_b64": base64.b64encode(b"%PDF-1.7 " + display_name.encode("utf-8")).decode("ascii"),
                    "filename": f"{base_filename}.pdf",
                    "content_type": "application/pdf",
                    "label": f"{display_name}.pdf",
                    "file_upload": True,
                }),
            ])
    return respond


def docx(filename: str, title: str) -> Respond:
    """A generated DOCX, the `file_upload` item `agent_worker_handler.py:227-240` reads."""
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(
            task_id=message.task_id, agent_id=message.recipient, result="docx_generated",
            delivery_items=[DeliveryItem(type="file_upload", data={
                "file_bytes_b64": base64.b64encode(b"PK\x03\x04 docx").decode("ascii"),
                "filename": filename,
                "title": title,
            })])
    return respond


StepLike = Union[Step, Respond]


def _as_step(step: StepLike) -> Step:
    return step if isinstance(step, Step) else Step(step)


class StandInAgent:
    """A scripted agent registered in the real coordinator under its per-user id.

    `play(*steps)` queues steps used one per call; `always(step)` answers every call after them. A step may be a `Step`
    or a bare answer (`says(...)`, `page(...)`, ...). `process` records every message in `received`; with no step left
    it appends the message to `unscripted` first, then raises `RuntimeError` (never an `AssertionError`: a strict xfail
    would take that for its expected failure). A step with a delegation calls `coordinator.handle_delegation` exactly
    as Smart's engine does (`{**message.context, "params": params}`, `delegation_engine.py:750-753,779-784`) before it
    answers."""

    def __init__(self, agent_id: str, agent_type: str, coordinator: AgentCoordinator) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.config = SimpleNamespace(capabilities=[])
        self.received: List[AgentMessage] = []
        self.unscripted: List[AgentMessage] = []
        self._coordinator = coordinator
        self._steps: List[Step] = []
        self._always: Optional[Step] = None

    def play(self, *steps: StepLike) -> "StandInAgent":
        self._steps.extend(_as_step(s) for s in steps)
        return self

    def always(self, step: StepLike) -> "StandInAgent":
        self._always = _as_step(step)
        return self

    async def process(self, message: AgentMessage) -> AgentResponse:
        self.received.append(message)
        if self._steps:
            step = self._steps.pop(0)
        elif self._always is not None:
            step = self._always
        else:
            self.unscripted.append(message)
            raise RuntimeError(f"no scripted step for {self.agent_id}")
        if step.delegate is not None:
            await self._coordinator.handle_delegation(
                intent=step.delegate.intent,
                query=step.delegate.query,
                context={**message.context, "params": dict(step.delegate.params)},
                calling_agent_id=self.agent_id,
            )
        return step.respond(message)


class SeamFactory:
    """The agent factory as `WorkerHandler` and `ConversationHandler` read it (`conversation_handler.py:461,477,915-916`):
    `ensure_agents_for_user` records, `get_session_store` answers the world's store, `user_repo` is the world's users."""

    def __init__(self, session_store: FirestoreSessionStore, user_repo: UserDirectory) -> None:
        self._session_store = session_store
        self.user_repo = user_repo
        self.ensured: List[str] = []

    async def ensure_agents_for_user(self, user_id: str) -> None:
        self.ensured.append(user_id)

    def get_session_store(self) -> FirestoreSessionStore:
        return self._session_store


# ----------------------------------------------------------------------------- the world

@dataclass(frozen=True)
class TaskRequest:
    """One created Cloud Task: its name's last path segment (None when unnamed), decoded body, `dispatch_deadline`
    seconds (None when unset) and whether it carries a `schedule_time`."""
    name: Optional[str]
    body: Dict[str, Any]
    deadline_s: Optional[int]
    scheduled: bool


class SeamWorld:
    """The module docstring's world. `worker_overrides` are `WorkerHandler` keywords added to or replacing the defaults
    (`email_review=`, `video_registry=`, `deep_research_fetch=`, ...)."""

    def __init__(self, monkeypatch: Any, **worker_overrides: Any) -> None:
        self.db = EvalDb()
        self.env = EnvironmentConfig()
        self.queue, self.client = make_failing_queue()
        self.dispatch = TaskDispatchService(self.queue)
        self.registry = AgentRegistry()
        for descriptor in ALL_DESCRIPTORS:
            self.registry.register(descriptor)
        self.coordinator = AgentCoordinator(registry=self.registry, task_queue=self.queue)
        self.state = FirestoreNotificationStateAdapter(self.db, self.env)
        self.sessions = FirestoreSessionStore(self.db, self.env.sessions_collection,
                                              max_history_length=SEAM_CONSOLIDATION.threshold,
                                              batch_size=SEAM_CONSOLIDATION.batch_size)
        self.channels = SeamChannels()
        self.media: Dict[str, RecordingMedia] = {"slack": RecordingMedia("slack"), "telegram": RecordingMedia("telegram")}
        self.notifier = UserNotificationService(self.state, self.channels, self.coordinator, NOTIFICATION_SLA,
                                                session_store=self.sessions)
        self.notifier.register_platform_media("slack", self.media["slack"])
        self.notifier.register_platform_media("telegram", self.media["telegram"])
        self.gcs = FakeGcs(buckets={BUCKET: "plain"})
        self.gcs.install(monkeypatch)
        self.media_store = GcsMediaAdapter(BUCKET)
        self.links = FileLinkService(FileAccessTokenService(SECRET), SERVICE_URL)
        self.doc_delivery = DocumentDeliveryService(self.media_store, self.links)
        self.users = UserDirectory()
        self.factory = SeamFactory(self.sessions, self.users)
        self.notes = FirestoreAgentNoteAdapter(self.db, self.env)
        self.reminders = RemindersService(self.notes, self.users, self.dispatch, DateutilRecurrenceAdapter(),
                                          dispatch_deadline_s=dispatch_deadline_s(NotificationKind.REMINDER))
        self.claims = FirestoreTaskClaimStore(db_client=self.db, collection_name=self.env.worker_task_dedup_collection,
                                              ttl_seconds=3600)
        self.agent_worker = AgentWorkerHandler(
            self.coordinator, notification_service=self.notifier, media_storage=self.media_store,
            task_queue=self.queue, doc_delivery_service=self.doc_delivery, link_service=self.links,
        )
        keywords: Dict[str, Any] = dict(
            agent_worker_handler=self.agent_worker, email_indexing_service=None, notification_service=self.notifier,
            consolidation_service=None, coordinator=self.coordinator, agent_factory=self.factory,
            indexed_email_repo=None, user_repo=self.users, task_dispatch=self.dispatch, media_storage=self.media_store,
            link_service=self.links, reminders_service=self.reminders, notes_port=self.notes, task_claims=self.claims,
        )
        keywords.update(worker_overrides)
        self.handler = WorkerHandler(**keywords)
        self._agents: Dict[str, StandInAgent] = {}
        self._runs = 0

    # -- agents ------------------------------------------------------------------------------------------------

    def agent(self, base_id: str) -> StandInAgent:
        """The stand-in registered as `f"{base_id}_{USER}"`, created and registered on first use."""
        if base_id not in self._agents:
            descriptor = self.registry.get_descriptor(base_id)
            agent_type = descriptor.agent_type if descriptor is not None else base_id.removesuffix("_agent")
            stand_in = StandInAgent(f"{base_id}_{USER}", agent_type, self.coordinator)
            self.coordinator.register_agent(stand_in)  # type: ignore[arg-type]
            self._agents[base_id] = stand_in
        return self._agents[base_id]

    def unscripted(self) -> List[AgentMessage]:
        """Every message a stand-in received with no step left, across stand-ins."""
        return [m for a in self._agents.values() for m in a.unscripted]

    # -- channels ----------------------------------------------------------------------------------------------

    async def set_primary(self, platform: str, channel: str) -> None:
        await self.state.save_primary(USER, platform, channel)

    async def set_last_active(self, platform: str, channel: str) -> None:
        await self.state.save(USER, platform, channel)

    def posts_to(self, platform: str, channel: str) -> List[Post]:
        """Every post on that channel, in order, of every kind."""
        return [p for p in self.channels.posts if (p.platform, p.channel_id) == (platform, channel)]

    def docs_to(self, platform: str, channel: str) -> List[Tuple[str, str]]:
        """The document links posted on that channel, as `(label, url)`."""
        return [p.value for p in self.posts_to(platform, channel) if p.kind == "doc"]

    # -- stores ------------------------------------------------------------------------------------------------

    def history(self, session_id: str) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """The stored history of a session as `(role, text, full_text)` of each message's first part; `[]` when the
        session has no document."""
        doc = self.db.collection(self.env.sessions_collection).docs.get(session_id)
        if doc is None:
            return []
        out: List[Tuple[str, Optional[str], Optional[str]]] = []
        for message in doc.get("history", []):
            part = (message.get("parts") or [{}])[0]
            out.append((message["role"], part.get("text"), part.get("full_text")))
        return out

    def stored_keys(self) -> List[str]:
        """The live object names in the media bucket, sorted."""
        return self.gcs.names(BUCKET)

    def requests(self, task_type: Optional[str] = None) -> List[TaskRequest]:
        """Every task created through the Cloud Tasks client, in order; only `task_type`'s when given."""
        out: List[TaskRequest] = []
        for request in self.client.create_task_requests:
            task = request["task"]
            body = json.loads(task["http_request"]["body"])
            if task_type is not None and body.get("task_type") != task_type:
                continue
            name = task.get("name")
            deadline = task.get("dispatch_deadline")
            out.append(TaskRequest(
                name=name.rsplit("/", 1)[-1] if name else None,
                body=body,
                deadline_s=deadline.seconds if deadline is not None else None,
                scheduled="schedule_time" in task,
            ))
        return out

    def bodies(self, task_type: Optional[str] = None) -> List[Dict[str, Any]]:
        return [r.body for r in self.requests(task_type)]

    async def run(self, body: Dict[str, Any]) -> Answer:
        """Deliver a created body through `/worker` as its own Cloud Task (closes a chain)."""
        self._runs += 1
        return await deliver(self.handler, body, name=f"seam-{self._runs}")

    async def seed_note(self, monkeypatch: Any, *, due: datetime, created_at: datetime,
                        complexity: Optional[TaskComplexity] = None, recurrence: Optional[str] = None) -> str:
        """Create the "call dad" note for `u1` through the real adapter (its id is `created_at`'s millisecond, so each
        note needs its own `created_at`). Returns the note id."""
        pin_datetime(monkeypatch, firestore_agent_note_adapter, created_at)
        note = await self.notes.create_note(NoteCreate(
            user_id=USER, text="call dad", instruction="Remind the user to call dad.",
            due=due, recurrence=recurrence, complexity=complexity,
        ))
        require(note.note_id == str(int(created_at.timestamp() * 1000)), "the note id is the creation millisecond")
        return note.note_id

    def chat_handler(self, **kw: Any) -> ConversationHandler:
        """The real `ConversationHandler` on this world (`conversation_handler.py:137-160`)."""
        return ConversationHandler(coordinator=self.coordinator, agent_factory=self.factory,  # type: ignore[arg-type]
                                   file_service=None,  # type: ignore[arg-type]
                                   notification_service=self.notifier, doc_delivery_service=self.doc_delivery,
                                   global_config=SEAM_CONSOLIDATION, **kw)


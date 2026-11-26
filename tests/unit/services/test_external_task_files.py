"""Task files (EXTERNAL_TASK_QUEUE_RFC §5.12, §6.6, §7): `ExternalTaskFiles` under `ExternalTaskService`.

The world: the service over the in-memory stores of `tests/support/external_task_stores.py`, the real
`FirestoreUserRepository` on `EvalDb`, the real `InactiveUserCheck`, the real `GcpTaskQueue` over a recording Cloud Tasks
client, and the real `ExternalTaskFiles` over the fakes of `tests/support/task_file_fakes.py` (a scripted resolver and
an in-memory media bucket with the port's semantics). Assertions are on what the task store and the bucket hold, what
the service returns and the Cloud Tasks bodies created.

Alex Doe is `usr-1` (account `acc-1`) with the workers `research` and `calendar-watch`; the files are `Tom's plan.docx`
(a drive item) and `birth-certificate.pdf` (an email attachment).
"""
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.document_open import ResolvedFile
from src.domain.email_open_texts import active_kind_refusal_note
from src.domain.exceptions import FileNotResolved
from src.domain.external_task import (
    FILE_LINK_TTL_S,
    LEASE_S,
    MAX_FILES_PER_TASK,
    ExternalTaskStatus,
    SubmitVia,
    TaskFile,
    TaskOrigin,
)
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_refusal import ExternalTaskRefused, TaskFileRefused, TaskRefusal
from src.domain.external_worker_principal import WorkerPrincipal
from src.domain.user import UserProfile
from src.domain.user_drive import DriveAccessRefused
from src.services import external_task_files as files_module
from src.services import external_task_service as service_module
from src.services.external_task_files import ExternalTaskFiles
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.external_task_world import NOTE
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue
from tests.support.task_file_fakes import (
    CERTIFICATE,
    CERTIFICATE_REF,
    PLAN,
    PLAN_REF,
    InMemoryMediaStorage,
    ScriptedFileSource,
)

T0 = UTC_MIDNIGHT.utc
S = ExternalTaskStatus
R = TaskRefusal
USERS = "users"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TITLE = "Plan review"
TEXT = "summarise the attached plan and check it against the certificate"
MISSING_REF = "workspace:references/Tom's plan.docx"   # never scripted: not found


class RecordingLogger:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def _add(self, message: str, *a: Any, **k: Any) -> None:
        self.lines.append(message % a if a else message)

    info = warning = error = debug = _add


class World:
    def __init__(self, *, files: bool = True) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = FakeExternalTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, self.client = make_task_queue()
        self.source = ScriptedFileSource()
        self.bucket = InMemoryMediaStorage()
        self.now = T0
        self._ids = 0
        self._bytes = 0
        self.service = ExternalTaskService(
            self.tasks, self.workers, self.queue, self.users, InactiveUserCheck(self.users),
            digest_key=bytes(range(32)), clock=lambda: self.now, new_id=self._new_id, random_bytes=self._random_bytes,
            files=ExternalTaskFiles(self.source, self.bucket) if files else None)

    def _new_id(self) -> str:
        self._ids += 1
        return f"id-{self._ids}"

    def _random_bytes(self, n: int) -> bytes:
        self._bytes += 1
        return bytes([self._bytes % 256]) * n

    def at(self, seconds: float) -> None:
        self.now = T0 + timedelta(seconds=seconds)

    async def seed(self) -> "World":
        for uid, acc, email in (("usr-1", "acc-1", "alex@example.com"), ("usr-2", "acc-2", "other@example.com")):
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=email, display_name=uid))
            await self.db.collection(USERS).document(uid).update({"config.external_tasks_enabled": True})
        self.research = await self.service.create_worker("usr-1", "research", "web research")
        self.watch = await self.service.create_worker("usr-1", "calendar-watch")
        self.alex = WorkerPrincipal("usr-1", "acc-1", self.research.worker_id, SubmitVia.KEY)
        self.other_worker = WorkerPrincipal("usr-1", "acc-1", self.watch.worker_id, SubmitVia.KEY)
        return self

    async def create(self, files=(PLAN_REF, CERTIFICATE_REF), entry: TaskEntry = TaskEntry.REMINDER):
        return await self.service.create("usr-1", "research", TITLE, TEXT, ORIGIN, entry=entry, note=NOTE,
                                         files=list(files))

    async def claimed(self, files=(PLAN_REF, CERTIFICATE_REF)):
        created = await self.create(files)
        claim = await self.service.claim(self.alex)
        assert claim is not None and claim.task_id == created.task_id
        return claim

    def timers(self) -> list:
        return [body for kind, body in self.client.worker_tasks() if kind == "external_task_timer"]


@pytest.fixture
async def world() -> World:
    return await World().seed()


async def refused(coro) -> TaskRefusal:
    with pytest.raises(ExternalTaskRefused) as caught:
        await coro
    return caught.value.reason


def key_of(task_id: str, number: int, name: str) -> str:
    return f"external_tasks/usr-1/{task_id}/{number}-{name}"


# ----------------------------------------------------------------------------------------------------- create

async def test_each_reference_is_resolved_for_sending_and_copied_once_under_the_tasks_key(world):
    created = await world.create()

    task = world.tasks.tasks[created.task_id]
    assert task.files == (
        TaskFile(number=1, name="Tom's plan.docx", mime=PLAN.mime, size=len(PLAN.data),
                 key=key_of(created.task_id, 1, "Tom's plan.docx")),
        TaskFile(number=2, name="birth-certificate.pdf", mime="application/pdf", size=len(CERTIFICATE.data),
                 key=key_of(created.task_id, 2, "birth-certificate.pdf")))
    assert world.bucket.objects == {
        key_of(created.task_id, 1, "Tom's plan.docx"): (PLAN.data, PLAN.mime),
        key_of(created.task_id, 2, "birth-certificate.pdf"): (CERTIFICATE.data, "application/pdf")}
    assert world.source.resolved == [(PLAN_REF, "usr-1", "send"), (CERTIFICATE_REF, "usr-1", "send")]
    assert created.files == task.files


async def test_only_an_email_attachment_goes_through_the_active_kind_check(world):
    await world.create()
    assert world.source.checked == ["birth-certificate.pdf"]


async def test_a_copy_is_what_the_owner_confirmed_even_when_the_source_changes_later(world):
    created = await world.create(entry=TaskEntry.CHAT)
    world.source.answers[PLAN_REF] = ResolvedFile(data=b"rewritten", name="Tom's plan.docx", mime=PLAN.mime)

    assert created.code is not None
    await world.service.confirm("usr-1", created.code)
    claim = await world.service.claim(world.alex)

    assert claim is not None and claim.files == created.files
    assert world.bucket.objects[key_of(created.task_id, 1, "Tom's plan.docx")][0] == PLAN.data
    assert world.source.resolved.count((PLAN_REF, "usr-1", "send")) == 1   # nothing read the source again


async def test_a_name_from_the_source_is_made_safe_for_the_key_and_the_record(world):
    world.source.answers[PLAN_REF] = ResolvedFile(data=b"x" * 10, name="../R&D/Tom's\nplan.docx", mime=PLAN.mime)
    created = await world.create(files=[PLAN_REF])
    [stored] = world.tasks.tasks[created.task_id].files
    assert stored.name == "Tom's_plan.docx"
    assert stored.key == key_of(created.task_id, 1, "Tom's_plan.docx")


async def test_one_unresolvable_reference_refuses_the_whole_create_and_stores_nothing(world):
    with pytest.raises(TaskFileRefused) as caught:
        await world.create(files=[PLAN_REF, MISSING_REF, CERTIFICATE_REF])

    assert (caught.value.reason, caught.value.ref, caught.value.line) == (R.FILE_UNRESOLVED, MISSING_REF, None)
    assert world.tasks.tasks == {} and world.bucket.objects == {} and world.timers() == []
    assert [ref for ref, _, _ in world.source.resolved] == [PLAN_REF, MISSING_REF]   # nothing read after it


@pytest.mark.parametrize("error", [
    FileNotResolved("[Tool note: 'Tom's plan.docx' is 50.0 MB; a file above 45 MB cannot be sent to the chat.]"),
    DriveAccessRefused("[Tool note: this drive does not allow reading that item.]"),
])
async def test_the_resolvers_own_answer_travels_with_the_refusal(world, error):
    world.source.answers[PLAN_REF] = error
    with pytest.raises(TaskFileRefused) as caught:
        await world.create()
    assert (caught.value.ref, caught.value.line) == (PLAN_REF, error.line)
    assert world.tasks.tasks == {} and world.bucket.objects == {}


async def test_an_email_attachment_the_active_check_refuses_refuses_the_create(world):
    world.source.active["birth-certificate.pdf"] = "macro_office"
    with pytest.raises(TaskFileRefused) as caught:
        await world.create()
    assert (caught.value.ref, caught.value.line) == (CERTIFICATE_REF, active_kind_refusal_note("macro_office"))
    assert world.tasks.tasks == {} and world.bucket.objects == {}


async def test_a_resolver_that_fails_refuses_the_create_naming_the_file(world):
    world.source.answers[CERTIFICATE_REF] = ConnectionError("the mailbox did not answer")
    with pytest.raises(TaskFileRefused) as caught:
        await world.create()
    assert (caught.value.ref, caught.value.line) == (CERTIFICATE_REF, None)
    assert world.tasks.tasks == {} and world.bucket.objects == {}


async def test_more_files_than_the_cap_are_refused_before_any_is_read(world):
    refs = [f"drive:p1:item-{n}" for n in range(MAX_FILES_PER_TASK + 1)]
    assert await refused(world.create(files=refs)) is R.FILES_CAP
    assert world.source.resolved == [] and world.tasks.tasks == {}


async def test_the_cap_count_itself_is_accepted(world):
    refs = [f"drive:p1:item-{n}" for n in range(MAX_FILES_PER_TASK)]
    world.source.answers.update({ref: PLAN for ref in refs})
    created = await world.create(files=refs)
    assert [f.number for f in world.tasks.tasks[created.task_id].files] == list(range(1, MAX_FILES_PER_TASK + 1))


async def test_files_over_the_total_cap_are_refused_and_nothing_is_stored(world, monkeypatch):
    monkeypatch.setattr(files_module, "TASK_FILES_MAX_BYTES", len(PLAN.data) + len(CERTIFICATE.data) - 1)
    assert await refused(world.create(files=[PLAN_REF, CERTIFICATE_REF, MISSING_REF])) is R.FILES_TOO_LARGE
    assert world.tasks.tasks == {} and world.bucket.objects == {}
    assert [ref for ref, _, _ in world.source.resolved] == [PLAN_REF, CERTIFICATE_REF]


async def test_files_exactly_at_the_total_cap_are_accepted(world, monkeypatch):
    monkeypatch.setattr(files_module, "TASK_FILES_MAX_BYTES", len(PLAN.data) + len(CERTIFICATE.data))
    created = await world.create()
    assert len(world.tasks.tasks[created.task_id].files) == 2


async def test_without_a_file_backend_a_create_with_files_is_refused_and_one_without_is_not():
    world = await World(files=False).seed()
    assert await refused(world.create()) is R.FILES_UNAVAILABLE
    assert world.tasks.tasks == {}
    created = await world.create(files=())
    assert world.tasks.tasks[created.task_id].files == ()


async def test_a_create_without_files_touches_neither_the_resolver_nor_the_bucket(world):
    created = await world.create(files=())
    assert world.tasks.tasks[created.task_id].files == () and created.files == ()
    assert world.source.resolved == [] and world.bucket.objects == {}


async def test_the_other_checks_run_before_any_file_is_read(world):
    assert await refused(world.service.create("usr-1", "nobody", TITLE, TEXT, ORIGIN, entry=TaskEntry.REMINDER,
                                              note=NOTE, files=[PLAN_REF])) is R.UNKNOWN_WORKER
    assert world.source.resolved == []


# --------------------------------------------------------------------------------------------- the worker's side

async def test_a_claim_hands_the_worker_the_files_of_the_task(world):
    claim = await world.claimed()
    assert [(f.number, f.name, f.mime, f.size) for f in claim.files] == [
        (1, "Tom's plan.docx", PLAN.mime, len(PLAN.data)),
        (2, "birth-certificate.pdf", "application/pdf", len(CERTIFICATE.data))]


async def test_a_live_lease_gets_a_fresh_signed_link_on_every_call(world):
    claim = await world.claimed()

    first = await world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 2)
    world.at(LEASE_S - 1)
    second = await world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 2)

    key = key_of(claim.task_id, 2, "birth-certificate.pdf")
    assert world.bucket.signed == [(key, FILE_LINK_TTL_S), (key, FILE_LINK_TTL_S)]
    assert first.url != second.url and first.url.startswith(f"{InMemoryMediaStorage.SIGNED_BASE}/{key}?")
    assert (first.task_id, first.number, first.name, first.mime, first.size, first.ttl_seconds) == (
        claim.task_id, 2, "birth-certificate.pdf", "application/pdf", len(CERTIFICATE.data), FILE_LINK_TTL_S)


async def test_a_lapsed_lease_is_refused_and_no_link_is_signed(world):
    claim = await world.claimed()
    world.at(LEASE_S)
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)) is R.LEASE_EXPIRED
    assert world.bucket.signed == []


async def test_another_lease_id_is_stale(world):
    claim = await world.claimed()
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, "another-lease", 1)) is R.STALE_LEASE


async def test_another_workers_task_is_not_found(world):
    claim = await world.claimed()
    assert await refused(world.service.get_task_file(
        world.other_worker, claim.task_id, claim.lease_id, 1)) is R.NOT_FOUND
    assert world.bucket.signed == []


async def test_a_held_task_is_not_found(world):
    held = await world.create(entry=TaskEntry.CHAT)
    assert await refused(world.service.get_task_file(world.alex, held.task_id, "any-lease", 1)) is R.NOT_FOUND


async def test_a_final_task_answers_as_a_missing_one(world):
    claim = await world.claimed()
    await world.service.submit(world.alex, claim.task_id, claim.lease_id, "The plan holds up.")
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)) is R.NOT_FOUND
    assert await refused(world.service.get_task_file(world.alex, "no-such-task", claim.lease_id, 1)) is R.NOT_FOUND
    assert world.bucket.signed == []


async def test_a_number_the_task_does_not_carry_is_no_such_file(world):
    claim = await world.claimed()
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 3)) is R.NO_SUCH_FILE


async def test_a_copy_that_is_no_longer_stored_is_file_gone(world):
    claim = await world.claimed()
    del world.bucket.objects[key_of(claim.task_id, 1, "Tom's plan.docx")]
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)) is R.FILE_GONE
    assert world.bucket.signed == []


@pytest.mark.parametrize("fault", ["fail_sign", "fail_exists"])
async def test_a_signing_or_storage_failure_is_a_retryable_answer(world, fault):
    claim = await world.claimed()
    setattr(world.bucket, fault, True)
    assert await refused(world.service.get_task_file(
        world.alex, claim.task_id, claim.lease_id, 1)) is R.LINK_UNAVAILABLE
    setattr(world.bucket, fault, False)
    link = await world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)   # asking again works
    assert link.number == 1


async def test_a_locked_or_switched_off_user_gets_no_link(world):
    claim = await world.claimed()
    await world.db.collection(USERS).document("usr-1").update({"config.external_tasks_enabled": False})
    assert await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)) is R.NOT_ENABLED


async def test_logs_name_the_task_and_the_file_number_never_the_link_the_name_or_the_key(world, monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(service_module, "logger", recorder)
    monkeypatch.setattr(files_module, "logger", recorder)
    claim = await world.claimed()
    link = await world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)
    del world.bucket.objects[key_of(claim.task_id, 2, "birth-certificate.pdf")]
    await refused(world.service.get_task_file(world.alex, claim.task_id, claim.lease_id, 2))
    world.source.answers[PLAN_REF] = ConnectionError("down")
    await refused(world.create())

    text = "\n".join(recorder.lines)
    assert f"task={claim.task_id} file=1" in text
    for secret in (link.url, "Tom's plan", "birth-certificate", "external_tasks/", PLAN_REF, CERTIFICATE_REF):
        assert secret not in text, secret


async def test_a_server_without_the_file_backend_gives_no_link_and_says_it_may_come_back(world):
    claim = await world.claimed()
    bare = ExternalTaskService(world.tasks, world.workers, world.queue, world.users, InactiveUserCheck(world.users),
                               digest_key=bytes(range(32)), clock=lambda: world.now)
    assert await refused(bare.get_task_file(world.alex, claim.task_id, claim.lease_id, 1)) is R.LINK_UNAVAILABLE

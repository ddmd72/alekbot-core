"""The proactive notice about files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ49, Δ53, Δ54).

Sent by the main service's fan-out, never by the indexer: only for a record whose `inaccessible_since` is older than
the start of a completed delta round (`observed_as_of`), so a file that was in fact deleted is gone first; once per
`inaccessible_since`; all of a pass's files in one message. The chat text is a fixed localized UI string with the count
and the fix only; the paths are always in an attached text file, so no file name reaches the chat text or a model; no
history pair. The list goes first and the text only once it was sent; the records are marked notified before the send
and unmarked when nothing was sent. The fan-out reads the records only while the tick's counter says some are waiting."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import List, Optional

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.support.drive_connection_service_fake import single_connection_repository
from tests.unit.services.drive_index_fakes import T0, Clock, FakeQueue

MARKED = T0 + timedelta(minutes=1)


class Notifier:
    def __init__(self) -> None:
        self.files: List[dict] = []
        self.texts: List[dict] = []
        self.file_sent = True
        self.text_sent = True
        self.order: List[str] = []
        self.flags_at_send: Optional[dict] = None
        self.repo: Optional[InMemoryDriveIndexRepository] = None

    async def notify_file_bytes(self, user_id, account_id, file_bytes, filename, title, **kw) -> bool:
        self.order.append("file")
        if self.repo is not None:
            self.flags_at_send = {k[1]: r.inaccessible_notified_since for k, r in self.repo.records.items()}
        self.files.append({"user_id": user_id, "bytes": file_bytes, "filename": filename, "title": title, **kw})
        return self.file_sent

    async def notify_raw(self, user_id, account_id, text, **kw) -> bool:
        self.order.append("text")
        self.texts.append({"user_id": user_id, "text": text, **kw})
        return self.text_sent

    async def notify(self, *a, **kw):   # the formatter path: never used for this notice (Δ53)
        raise AssertionError("the inaccessible notice never goes through a formatter")


class Localization:
    def get_ui_string(self, lang, message):
        assert lang is LanguageCode.UK
        return {UIMessage.DRIVE_INACCESSIBLE_NOTICE: "{count} closed; copy each file in the same folder",
                UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE: "Closed files"}[message]


class Language:
    async def resolve_ui_language(self, user_id):
        return LanguageCode.UK


def closed(item_id, name, parent="root", since=MARKED, notified=None, folder=False):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, parent_id=parent, is_folder=folder,
                            kind="folder" if folder else "pdf", scope=Scope.NORMAL,
                            state=RecordState.METADATA_ONLY if folder else RecordState.PENDING, has_card=True,
                            inaccessible_since=since, inaccessible_checked_at=since,
                            inaccessible_notified_since=notified)


@pytest.fixture
def env():
    clock = Clock()
    clock.now = T0 + timedelta(hours=1)
    repo = InMemoryDriveIndexRepository()
    notifier = Notifier()
    notifier.repo = repo
    queue = FakeQueue()

    svc = DriveSyncSchedulerService(repo=repo, queue=queue, connections=single_connection_repository("u"),
                                    notifier=notifier, alert_sink=None,
                                    clock=clock, localization=Localization(), language=Language())
    return SimpleNamespace(svc=svc, repo=repo, notifier=notifier, clock=clock)


async def state(env, observed=MARKED + timedelta(minutes=10), waiting=1, **kw):
    env.repo.states["u"] = DriveSyncState(user_id="u", connection_id="u", root_item_id="root", observed_as_of=observed,
                                          created_at=T0, last_success_at=env.clock.now,
                                          counters={INACCESSIBLE_COUNTER: waiting,
                                                    INACCESSIBLE_UNNOTIFIED_COUNTER: waiting}, **kw)


async def seed(env, *recs):
    await env.repo.upsert_records(list(recs))


async def test_no_notice_before_a_later_complete_round(env):
    await seed(env, closed("f1", "id-scan.pdf"))
    await state(env, observed=MARKED)            # the round that saw the marking started before it: not yet
    await env.svc.fan_out()
    assert env.notifier.files == [] and env.notifier.texts == []
    await state(env, observed=MARKED + timedelta(seconds=1))
    await env.svc.fan_out()
    assert len(env.notifier.files) == 1 and len(env.notifier.texts) == 1


async def test_one_notice_for_the_batch_text_without_names_and_the_list_as_a_file(env):
    folder = closed("d1", "Shared docs", folder=True)
    await seed(env, folder, closed("f1", "id-scan.pdf", parent="d1"), closed("f2", "citizen card.pdf"))
    await state(env, waiting=2)
    summary = await env.svc.fan_out()
    assert summary["notices"] == 1 and env.notifier.order == ["file", "text"]
    text = env.notifier.texts[0]["text"]
    assert text == "2 closed; copy each file in the same folder"
    assert "scan" not in text and "citizen" not in text and "Shared" not in text
    listing = env.notifier.files[0]["bytes"].decode("utf-8").splitlines()
    assert sorted(listing) == ["Shared docs/id-scan.pdf", "citizen card.pdf"]   # files only, never the folder
    assert env.notifier.files[0]["title"] == "Closed files" and env.notifier.files[0]["filename"].endswith(".txt")
    recs = env.repo.records
    assert recs[("u", "f1")].inaccessible_notified_since == MARKED == recs[("u", "f2")].inaccessible_notified_since


async def test_flags_go_up_before_the_send(env):
    await seed(env, closed("f1", "a.pdf"))
    await state(env)
    await env.svc.fan_out()
    assert env.notifier.flags_at_send == {"f1": MARKED}


async def test_flags_come_down_when_nothing_was_sent(env):
    await seed(env, closed("f1", "a.pdf"))
    await state(env)
    reads = []
    original = env.repo.inaccessible_records

    async def spy(user_id, limit, **kw):
        reads.append(env.clock.now)
        return await original(user_id, limit, **kw)
    env.repo.inaccessible_records = spy
    env.notifier.file_sent = False
    summary = await env.svc.fan_out()
    assert summary["notices"] == 0 and env.notifier.texts == []   # no list sent: no text either
    assert env.repo.records[("u", "f1")].inaccessible_notified_since is None
    assert len(reads) == 1 and len(env.notifier.files) == 1
    # Δ76: undeliverable, so held for the rest of the UTC day while the tick counts nothing new: no read, no send
    env.notifier.file_sent = True
    env.clock.advance(600)
    assert (await env.svc.fan_out())["notices"] == 0
    assert len(reads) == 1 and len(env.notifier.files) == 1 and env.notifier.texts == []
    assert env.repo.records[("u", "f1")].inaccessible_notified_since is None
    # retried once the UTC day turns
    next_day = (env.clock.now + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)
    env.clock.advance((next_day - env.clock.now).total_seconds())
    env.repo.states["u"] = replace(env.repo.states["u"], last_success_at=env.clock.now)
    await env.svc.fan_out()
    assert len(reads) == 2 and len(env.notifier.files) == 2 and len(env.notifier.texts) == 1
    assert env.repo.records[("u", "f1")].inaccessible_notified_since == MARKED


async def test_never_again_for_the_same_since(env):
    await seed(env, closed("f1", "a.pdf"))
    await state(env)
    await env.svc.fan_out()
    await env.svc.fan_out()   # the tick has not refreshed the counter yet: read, but nothing new
    assert len(env.notifier.files) == 1 and len(env.notifier.texts) == 1


async def test_a_new_transition_notifies_again(env):
    await seed(env, closed("f1", "a.pdf", since=MARKED, notified=T0 - timedelta(days=3)))   # an older transition
    await state(env)
    await env.svc.fan_out()
    assert len(env.notifier.texts) == 1 and env.repo.records[("u", "f1")].inaccessible_notified_since == MARKED


async def test_no_query_while_the_tick_counts_nothing_waiting(env):
    await seed(env, closed("f1", "a.pdf"))
    await state(env, waiting=0)
    calls = []
    original = env.repo.inaccessible_records

    async def spy(user_id, limit):
        calls.append(user_id)
        return await original(user_id, limit)
    env.repo.inaccessible_records = spy
    await env.svc.fan_out()
    assert calls == [] and env.notifier.files == []


async def test_a_text_that_fails_after_the_list_keeps_the_flags(env):
    await seed(env, closed("f1", "a.pdf"))
    await state(env)
    env.notifier.text_sent = False
    await env.svc.fan_out()
    assert env.repo.records[("u", "f1")].inaccessible_notified_since == MARKED   # the list reached the user

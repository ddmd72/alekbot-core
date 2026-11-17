"""`build_background_jobs`: the agent worker delivers through the file-delivery bundle and the given notification service,
and reminders dispatch through the one task dispatch."""
import dataclasses
from types import SimpleNamespace

import pytest

from src.composition.background_jobs_setup import build_background_jobs
from src.config.environment import EnvironmentConfig
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import dispatch_deadline_s
from tests.support.firestore_eval import EvalDb

COORDINATOR, NOTIFY, USERS = object(), object(), object()
DELIVERY = SimpleNamespace(media_storage=object(), doc_delivery_service=object(), link_service=object(),
                           short_link_service=object())
LANGUAGES = SimpleNamespace(localization=object(), resolver=object())


def _container(*, notes=True):
    return SimpleNamespace(user_drive_service=object(), notes_adapter=object() if notes else None,
                           recurrence_adapter=object())


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    env = EnvironmentConfig()

    def run(*, queue="QUEUE", container=None):
        container = container or _container()
        db = EvalDb()
        jobs = build_background_jobs(container=container, coordinator=COORDINATOR,  # type: ignore[arg-type]
                                     notification_service=NOTIFY, task_queue=queue, delivery=DELIVERY,
                                     user_repo=USERS, languages=LANGUAGES)
        return SimpleNamespace(jobs=jobs, container=container, db=db, env=env)
    return run


def test_the_agent_worker_delivers_through_the_bundles_files_and_the_notification_service(build):
    built = build()
    handler = built.jobs.agent_worker_handler

    assert (handler._coordinator, handler._notification, handler._task_queue) == (COORDINATOR, NOTIFY, "QUEUE")
    assert (handler._media_storage, handler._doc_delivery_service, handler._link_service,
            handler._short_link_service) == (DELIVERY.media_storage, DELIVERY.doc_delivery_service,
                                             DELIVERY.link_service, DELIVERY.short_link_service)
    copy = handler._deep_research_drive_copy
    assert (copy._users, copy._drive) == (USERS, built.container.user_drive_service)
    assert copy._localization is LANGUAGES.localization and copy._language is LANGUAGES.resolver


def test_reminders_dispatch_through_the_one_task_dispatch_on_the_reminder_deadline(build):
    built = build()
    jobs, container = built.jobs, built.container

    assert jobs.task_dispatch._queue == "QUEUE"
    reminders = jobs.reminders
    assert reminders._task_dispatch is jobs.task_dispatch and reminders._notes_port is container.notes_adapter
    assert reminders._user_repo is USERS and reminders._recurrence is container.recurrence_adapter
    assert reminders._dispatch_deadline_s == dispatch_deadline_s(NotificationKind.REMINDER)


def test_without_a_task_queue_there_is_no_dispatch_and_no_reminders(build):
    jobs = build(queue=None).jobs

    assert jobs.task_dispatch is None and jobs.reminders is None
    assert jobs.agent_worker_handler._task_queue is None


def test_without_a_notes_store_there_are_no_reminders(build):
    jobs = build(container=_container(notes=False)).jobs

    assert jobs.task_dispatch is not None and jobs.reminders is None


def test_the_bundle_is_frozen(build):
    with pytest.raises(dataclasses.FrozenInstanceError):
        build().jobs.reminders = None  # type: ignore[misc]

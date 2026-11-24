"""`ExternalTaskService.is_owner`: the Send button's owner check. True only for a task of that user, in any status; no
lock or flag read (the click is authorised by IAM first, and the release is `confirm`, with both checks).

The world of `tests/support/external_task_world.py`; Alex Doe is `usr-1`, `usr-2` is another user."""
from __future__ import annotations

import pytest

from src.domain.external_task_entry import TaskEntry
from src.domain.user import InactiveReason
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, USERS, ExternalTaskWorld


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def test_a_task_is_its_owners_held_and_after_it_was_sent(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)
    assert await world.service.is_owner(USER_ID, created.task_id) is True

    await world.service.confirm(USER_ID, created.code)

    assert await world.service.is_owner(USER_ID, created.task_id) is True


async def test_another_users_task_and_a_missing_task_are_not(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)

    assert await world.service.is_owner("usr-2", created.task_id) is False
    assert await world.service.is_owner(USER_ID, "t1") is False


async def test_it_reads_neither_the_flag_nor_the_lock(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)
    await world.flag(USER_ID, False)
    await world.db.collection(USERS).document(USER_ID).update(
        {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})

    assert await world.service.is_owner(USER_ID, created.task_id) is True

"""Wire — errors (GOOGLE_DRIVE_RFC §3.7, GE-18): a failed Drive call is classified by the body's `reason` before its
status. Rate limits (403 `userRateLimitExceeded`/`rateLimitExceeded`), 429 and 5xx are retried 3 times with truncated
exponential backoff (at most 32 s) and then DriveThrottled; permission reasons are DriveAccessDenied; the daily limit is
DriveThrottled(TICK_DEADLINE_S) at once; storage, depth and 404 map to their port errors; a 401 gets one forced refresh;
any other 4xx is a ValueError carrying only the status and the reason. Served by the stateful Drive fake."""
import asyncio
import logging

import pytest

from src.adapters.google.google_drive_adapter import MAX_BACKOFF_S, RETRIES
from src.adapters.google.google_drive_mapping import classify, error_reason
from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.user_drive import (
    DriveAccessDenied, DriveItemNotFoundError, DrivePathTooLong, DriveReauthRequired, DriveStorageFull, DriveThrottled,
)
from tests.support.google_drive_fake import error_body, google_setup

_RETRIED = [(403, "userRateLimitExceeded"), (403, "rateLimitExceeded"), (429, ""), (500, ""), (503, "backendError")]


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


async def _file(setup):
    return setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")


@pytest.mark.parametrize("status,reason", _RETRIED)
async def test_rate_limits_and_server_errors_are_retried_then_throttled(setup, status, reason):
    node = await _file(setup)
    setup.fake.fail("files.get", status, reason, times=RETRIES + 1)
    with pytest.raises(DriveThrottled) as caught:
        await setup.adapter.get_item(setup.handle, node.id)
    assert len(setup.fake.calls("files.get")) == RETRIES + 1
    assert setup.sleeps == [1.0, 2.0, 4.0] and all(s <= MAX_BACKOFF_S for s in setup.sleeps)
    assert caught.value.retry_after_s > 0


@pytest.mark.parametrize("status,reason", _RETRIED)
async def test_a_retried_call_that_recovers_answers(setup, status, reason):
    node = await _file(setup)
    setup.fake.fail("files.get", status, reason, times=RETRIES)
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert item.item_id == node.id and len(setup.sleeps) == RETRIES


async def test_the_backoff_is_truncated_at_32_seconds(setup):
    node = await _file(setup)
    setup.fake.fail("files.get", 429, "", times=RETRIES + 1, headers={"Retry-After": "600"})
    with pytest.raises(DriveThrottled) as caught:
        await setup.adapter.get_item(setup.handle, node.id)
    assert setup.sleeps == [MAX_BACKOFF_S] * RETRIES
    assert caught.value.retry_after_s == 600


@pytest.mark.parametrize("reason", ["insufficientFilePermissions", "appNotAuthorizedToFile", "domainPolicy"])
async def test_permission_reasons_are_access_denied_without_retry(setup, reason):
    node = await _file(setup)
    setup.fake.fail("files.get", 403, reason)
    with pytest.raises(DriveAccessDenied):
        await setup.adapter.get_item(setup.handle, node.id)
    assert len(setup.fake.calls("files.get")) == 1 and setup.sleeps == []


async def test_a_403_is_read_by_reason_never_by_status():
    assert classify(403, "userRateLimitExceeded") != classify(403, "insufficientFilePermissions")
    assert classify(403, "") == classify(418, "")             # a bare 403 is neither a rate limit nor a refusal
    assert error_reason('{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}') == "rateLimitExceeded"
    assert error_reason("<html>busy</html>") == "" and error_reason('{"error": {"errors": [{"reason": "a b"}]}}') == ""


async def test_the_daily_limit_is_throttled_for_a_whole_tick_without_retry(setup):
    node = await _file(setup)
    setup.fake.fail("files.get", 403, "dailyLimitExceeded")
    with pytest.raises(DriveThrottled) as caught:
        await setup.adapter.get_item(setup.handle, node.id)
    assert caught.value.retry_after_s == TICK_DEADLINE_S
    assert len(setup.fake.calls("files.get")) == 1 and setup.sleeps == []


async def test_storage_quota_is_storage_full(setup):
    setup.fake.fail("upload.multipart", 403, "storageQuotaExceeded")
    with pytest.raises(DriveStorageFull):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "brief.docx", b"x", "application/octet-stream")


async def test_the_hierarchy_depth_limit_is_path_too_long(setup):
    setup.fake.fail("files.create", 403, "myDriveHierarchyDepthLimitExceeded")
    with pytest.raises(DrivePathTooLong):
        await setup.adapter.create_folder(setup.handle, setup.a.root.id, "Q3")


async def test_404_is_not_found(setup):
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.get_item(setup.handle, "fid-missing")
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.get_change(setup.handle, "fid-missing")


async def test_a_401_gets_one_forced_refresh_and_the_call_is_repeated_with_the_new_token(setup):
    node = await _file(setup)
    setup.fake.fail("files.get", 401, "authError")
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert item.item_id == node.id
    assert len(setup.fake.calls("token")) == 1
    bearers = [r.bearer for r in setup.fake.calls("files.get")]
    assert bearers[0] == "tok-a" and bearers[1].startswith("tok-perm-a-")


async def test_a_401_after_a_good_refresh_is_access_denied(setup):
    node = await _file(setup)
    setup.fake.fail("files.get", 401, "authError", times=2)
    with pytest.raises(DriveAccessDenied):
        await setup.adapter.get_item(setup.handle, node.id)
    assert len(setup.fake.calls("token")) == 1


async def test_a_401_whose_refresh_is_rejected_is_reauth_required(setup):
    node = await _file(setup)
    setup.fake.refresh_tokens.clear()
    setup.fake.fail("files.get", 401, "authError")
    with pytest.raises(DriveReauthRequired):
        await setup.adapter.get_item(setup.handle, node.id)


@pytest.mark.parametrize("status,reason", [(403, "numChildrenInNonRootLimitExceeded"), (409, "conflict"),
                                           (403, "")])
async def test_any_other_4xx_is_a_value_error_with_status_and_reason_only(setup, caplog, status, reason):
    node = await _file(setup)
    setup.fake.fail("files.get", status, reason)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as caught:
            await setup.adapter.get_item(setup.handle, node.id)
    text = str(caught.value)
    assert str(status) in text and reason in text
    message = error_body(status, reason)["error"]["message"]
    assert message not in text and "Garden plan" not in caplog.text
    assert setup.sleeps == []


async def test_a_timeout_and_a_transport_failure_keep_their_classes(setup, caplog):
    node = await _file(setup)
    setup.fake.fail("files.get", "timeout")
    with pytest.raises(asyncio.TimeoutError):
        await setup.adapter.get_item(setup.handle, node.id)
    setup.fake.fail("files.get", "transport")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(OSError):
            await setup.adapter.get_item(setup.handle, node.id)
    assert "fields" not in caplog.text                       # the call's URL and query are never logged

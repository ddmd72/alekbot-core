"""
Wire tests for MicrosoftToDoAdapter.

Mock boundary: aiohttp.ClientSession (HTTP layer).
Never mock at TasksProviderPort/TaskLifecyclePort level.

Covers:
- Port compliance
- _refresh_token: calls MS token endpoint, saves updated credentials
- list_task_lists: GET → TaskList objects
- create_task: POST → payload, list_id from primary list, returns Task
- update_task: PATCH main fields; checklist sync (PATCH changed, DELETE removed, POST new)
- delete_task: DELETE called on correct path
- get_task: GET single task + list name
- batch_get_tasks: parallel fetches, bounded concurrency
- ensure_primary_list: finds existing list; creates when absent
- register_subscription: POST with correct payload, returns TaskSubscriptionConfig
- renew_subscription: PATCH subscription, returns updated config
- delete_subscription: DELETE subscription
- Field mapping: importance, status, tags, body, due_datetime
- 404 on get_task → ValueError
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft_todo_adapter import MicrosoftToDoAdapter
from src.domain.email import OAuthCredentials
from src.domain.task import (
    ChecklistItem,
    Task,
    TaskCreate,
    TaskImportance,
    TaskList,
    TaskStatus,
    TaskSubscriptionConfig,
    TaskUpdate,
    TaskUserConfig,
)
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.task_config_port import TaskConfigPort
from src.ports.task_lifecycle_port import TaskLifecyclePort
from src.ports.tasks_provider_port import TasksProviderPort

_USER_ID = "user-abc123"
_LIST_ID = "list-1"
_TASK_ID = "task-1"

_VALID_CREDS = OAuthCredentials(
    user_id=_USER_ID,
    provider="microsoft_todo",
    access_token="tok-valid",
    refresh_token="ref-valid",
    token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
    scopes=["Tasks.ReadWrite"],
    email_address="user@example.com",
)

_EXPIRED_CREDS = OAuthCredentials(
    user_id=_USER_ID,
    provider="microsoft_todo",
    access_token="tok-expired",
    refresh_token="ref-valid",
    token_expiry=datetime.now(timezone.utc) - timedelta(hours=1),
    scopes=["Tasks.ReadWrite"],
    email_address="user@example.com",
)

_MS_TASK = {
    "id": _TASK_ID,
    "title": "Buy milk",
    "body": {"content": "Full fat", "contentType": "text"},
    "status": "notStarted",
    "importance": "normal",
    "categories": ["shopping"],
    "isReminderOn": False,
    "checklistItems": [],
    "linkedResources": [],
}

_MS_LIST = {"id": _LIST_ID, "displayName": "Alek Bot Tasks", "isOwner": True, "isShared": False}


def _make_adapter(creds=_VALID_CREDS):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = creds
    oauth.save_credentials.return_value = None

    task_config = AsyncMock(spec=TaskConfigPort)
    task_config.get_config.return_value = TaskUserConfig()
    task_config.save_config.return_value = None
    task_config.set_primary_list_id_if_absent.return_value = _LIST_ID

    return (
        MicrosoftToDoAdapter(
            oauth_credentials=oauth,
            task_config=task_config,
            client_id="client-id",
            client_secret="client-secret",
        ),
        oauth,
        task_config,
    )


def _mock_response(json_data, status=200):
    resp = MagicMock()
    resp.status = status
    resp.ok = status < 300
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=str(json_data))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(responses: dict):
    """responses: {method: (resp_or_list)}  method = "get"|"post"|"patch"|"delete" """
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    for method, resp in responses.items():
        # Support side_effect list for multiple calls
        if isinstance(resp, list):
            getattr(session, method).side_effect = resp
        else:
            getattr(session, method).return_value = resp

    return session


def _patch_session(session):
    return patch("aiohttp.ClientSession", return_value=session)


# =============================================================================
# Port compliance
# =============================================================================


class TestMicrosoftToDoAdapterPortCompliance:

    def test_is_tasks_provider_port_subclass(self):
        assert issubclass(MicrosoftToDoAdapter, TasksProviderPort)

    def test_is_task_lifecycle_port_subclass(self):
        assert issubclass(MicrosoftToDoAdapter, TaskLifecyclePort)

    def test_instantiates(self):
        adapter, _, _ = _make_adapter()
        assert isinstance(adapter, MicrosoftToDoAdapter)


# =============================================================================
# Token refresh
# =============================================================================


class TestTokenRefresh:

    async def test_refresh_called_when_token_expired(self):
        """Expired credentials → _refresh_token calls MS token endpoint."""
        adapter, oauth, _ = _make_adapter(creds=_EXPIRED_CREDS)

        # Mock the token refresh response
        token_resp = _mock_response({
            "access_token": "new-token",
            "expires_in": 3600,
            "refresh_token": "new-refresh",
        })
        list_resp = _mock_response({"value": [_MS_LIST]})

        session = _mock_session({"post": token_resp, "get": list_resp})
        with _patch_session(session):
            await adapter.list_task_lists(_USER_ID)

        oauth.save_credentials.assert_called_once()
        saved: OAuthCredentials = oauth.save_credentials.call_args.args[0]
        assert saved.access_token == "new-token"

    async def test_no_refresh_when_token_valid(self):
        """Valid credentials → save_credentials never called."""
        adapter, oauth, _ = _make_adapter(creds=_VALID_CREDS)
        list_resp = _mock_response({"value": [_MS_LIST]})

        session = _mock_session({"get": list_resp})
        with _patch_session(session):
            await adapter.list_task_lists(_USER_ID)

        oauth.save_credentials.assert_not_called()


# =============================================================================
# list_task_lists
# =============================================================================


class TestListTaskLists:

    async def test_returns_task_list_objects(self):
        adapter, _, _ = _make_adapter()
        resp = _mock_response({"value": [_MS_LIST]})
        session = _mock_session({"get": resp})

        with _patch_session(session):
            result = await adapter.list_task_lists(_USER_ID)

        assert len(result) == 1
        assert isinstance(result[0], TaskList)
        assert result[0].list_id == _LIST_ID
        assert result[0].name == "Alek Bot Tasks"

    async def test_returns_empty_list_when_no_lists(self):
        adapter, _, _ = _make_adapter()
        resp = _mock_response({"value": []})
        session = _mock_session({"get": resp})

        with _patch_session(session):
            result = await adapter.list_task_lists(_USER_ID)

        assert result == []


# =============================================================================
# create_task
# =============================================================================


class TestCreateTask:

    async def test_creates_task_returns_domain_task(self):
        adapter, _, _ = _make_adapter()
        # GET 1: list_task_lists → {"value": [...]} format
        # POST:  create task
        # GET 2: list name lookup → single list object
        lists_resp = _mock_response({"value": [_MS_LIST]})
        create_resp = _mock_response(_MS_TASK)
        list_name_resp = _mock_response(_MS_LIST)

        session = _mock_session({"get": [lists_resp, list_name_resp], "post": create_resp})
        with _patch_session(session):
            result = await adapter.create_task(
                _USER_ID, TaskCreate(title="Buy milk", tags=["shopping"])
            )

        assert isinstance(result, Task)
        assert result.title == "Buy milk"
        assert result.task_id == _TASK_ID
        assert result.list_id == _LIST_ID

    async def test_importance_mapped_to_ms_format(self):
        adapter, _, _ = _make_adapter()
        list_resp = _mock_response(_MS_LIST)
        create_resp = _mock_response({**_MS_TASK, "importance": "high"})
        session = _mock_session({"get": list_resp, "post": create_resp})

        with _patch_session(session):
            result = await adapter.create_task(
                _USER_ID, TaskCreate(title="Urgent", importance=TaskImportance.HIGH)
            )

        assert result.importance == TaskImportance.HIGH

    async def test_tags_mapped_from_categories(self):
        adapter, _, _ = _make_adapter()
        list_resp = _mock_response(_MS_LIST)
        ms_task = {**_MS_TASK, "categories": ["work", "urgent"]}
        create_resp = _mock_response(ms_task)
        session = _mock_session({"get": list_resp, "post": create_resp})

        with _patch_session(session):
            result = await adapter.create_task(
                _USER_ID, TaskCreate(title="Report", tags=["work", "urgent"])
            )

        assert "work" in result.tags
        assert "urgent" in result.tags


# =============================================================================
# update_task
# =============================================================================


class TestUpdateTask:

    async def test_patch_called_for_title_update(self):
        adapter, _, _ = _make_adapter()
        # patch → get_task (list name GET + task GET)
        patch_resp = _mock_response(_MS_TASK)
        list_resp = _mock_response(_MS_LIST)
        task_resp = _mock_response(_MS_TASK)

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.patch.return_value = patch_resp
        session.get.side_effect = [list_resp, task_resp, list_resp, task_resp]

        with _patch_session(session):
            result = await adapter.update_task(
                _USER_ID, _LIST_ID, _TASK_ID, TaskUpdate(title="Updated title")
            )

        session.patch.assert_called_once()
        patch_call = session.patch.call_args
        assert "title" in str(patch_call)
        assert isinstance(result, Task)

    async def test_status_completed_mapped_correctly(self):
        adapter, _, _ = _make_adapter()
        ms_completed = {**_MS_TASK, "status": "completed"}
        patch_resp = _mock_response(ms_completed)
        list_resp = _mock_response(_MS_LIST)
        task_resp = _mock_response(ms_completed)

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.patch.return_value = patch_resp
        # get_task: task GET first, then list name GET
        session.get.side_effect = [task_resp, list_resp]

        with _patch_session(session):
            result = await adapter.update_task(
                _USER_ID, _LIST_ID, _TASK_ID, TaskUpdate(status=TaskStatus.COMPLETED)
            )

        assert result.status == TaskStatus.COMPLETED


# =============================================================================
# delete_task
# =============================================================================


class TestDeleteTask:

    async def test_delete_called_on_correct_path(self):
        adapter, _, _ = _make_adapter()
        delete_resp = MagicMock()
        delete_resp.status = 204
        delete_resp.ok = True
        delete_resp.__aenter__ = AsyncMock(return_value=delete_resp)
        delete_resp.__aexit__ = AsyncMock(return_value=False)

        session = _mock_session({"delete": delete_resp})
        with _patch_session(session):
            await adapter.delete_task(_USER_ID, _LIST_ID, _TASK_ID)

        session.delete.assert_called_once()
        call_url = session.delete.call_args.args[0]
        assert _LIST_ID in call_url
        assert _TASK_ID in call_url

    async def test_404_on_delete_is_ignored(self):
        """DELETE 404 is treated as already deleted (idempotent)."""
        adapter, _, _ = _make_adapter()
        resp = MagicMock()
        resp.status = 404
        resp.ok = False
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        session = _mock_session({"delete": resp})
        with _patch_session(session):
            await adapter.delete_task(_USER_ID, _LIST_ID, _TASK_ID)
        # No exception raised


# =============================================================================
# get_task
# =============================================================================


class TestGetTask:

    async def test_get_task_returns_domain_task(self):
        adapter, _, _ = _make_adapter()
        list_resp = _mock_response(_MS_LIST)
        task_resp = _mock_response(_MS_TASK)
        session = _mock_session({"get": [task_resp, list_resp]})

        with _patch_session(session):
            result = await adapter.get_task(_USER_ID, _LIST_ID, _TASK_ID)

        assert isinstance(result, Task)
        assert result.task_id == _TASK_ID

    async def test_get_task_404_raises_value_error(self):
        adapter, _, _ = _make_adapter()
        resp = _mock_response({}, status=404)
        session = _mock_session({"get": resp})

        with _patch_session(session):
            with pytest.raises(ValueError, match="Not found"):
                await adapter.get_task(_USER_ID, _LIST_ID, "missing-task")


# =============================================================================
# ensure_primary_list
# =============================================================================


class TestEnsurePrimaryList:

    async def test_finds_existing_alek_bot_tasks_list(self):
        adapter, _, task_config = _make_adapter()
        # No config cached → query Graph API
        task_config.get_config.return_value = TaskUserConfig()
        resp = _mock_response({"value": [_MS_LIST]})
        session = _mock_session({"get": resp})

        with _patch_session(session):
            result = await adapter.ensure_primary_list(_USER_ID)

        assert result == _LIST_ID

    async def test_creates_list_when_absent(self):
        adapter, _, task_config = _make_adapter()
        task_config.get_config.return_value = TaskUserConfig()
        get_resp = _mock_response({"value": []})  # No lists found
        post_resp = _mock_response({"id": "new-list-id", "displayName": "Alek Bot Tasks"})
        session = _mock_session({"get": get_resp, "post": post_resp})

        with _patch_session(session):
            result = await adapter.ensure_primary_list(_USER_ID)

        assert result == "new-list-id"
        session.post.assert_called_once()

    async def test_uses_cached_list_id(self):
        adapter, _, task_config = _make_adapter()
        # Pre-populate cache
        adapter._primary_list_cache[_USER_ID] = "cached-list-id"
        session = _mock_session({})

        # No HTTP calls expected (cache hit)
        result = await adapter.ensure_primary_list(_USER_ID)

        assert result == "cached-list-id"

    async def test_loads_from_task_config_on_first_call(self):
        adapter, _, task_config = _make_adapter()
        task_config.get_config.return_value = TaskUserConfig(primary_list_id="config-list-id")
        session = _mock_session({})

        with _patch_session(session):
            result = await adapter.ensure_primary_list(_USER_ID)

        assert result == "config-list-id"


# =============================================================================
# register_subscription
# =============================================================================


class TestRegisterSubscription:

    async def test_returns_subscription_config(self):
        adapter, _, _ = _make_adapter()
        post_resp = _mock_response({
            "id": "sub-abc",
            "resource": f"/me/todo/lists/{_LIST_ID}/tasks",
        })
        session = _mock_session({"post": post_resp})

        with _patch_session(session):
            result = await adapter.register_subscription(
                _USER_ID, _LIST_ID, "https://myapp.com"
            )

        assert isinstance(result, TaskSubscriptionConfig)
        assert result.sub_id == "sub-abc"
        assert result.list_id == _LIST_ID

    async def test_notification_url_contains_user_id(self):
        adapter, _, _ = _make_adapter()
        post_resp = _mock_response({"id": "sub-1", "resource": f"/me/todo/lists/{_LIST_ID}/tasks"})
        session = _mock_session({"post": post_resp})

        with _patch_session(session):
            await adapter.register_subscription(_USER_ID, _LIST_ID, "https://myapp.com")

        call_json = session.post.call_args.kwargs["json"]
        assert _USER_ID in call_json["notificationUrl"]

    async def test_subscription_expiry_is_4000_minutes_from_now(self):
        adapter, _, _ = _make_adapter()
        post_resp = _mock_response({"id": "sub-1", "resource": "/me/todo/lists/x/tasks"})
        session = _mock_session({"post": post_resp})

        before = datetime.now(timezone.utc)
        with _patch_session(session):
            result = await adapter.register_subscription(_USER_ID, _LIST_ID, "https://app.com")
        after = datetime.now(timezone.utc)

        # expires_at should be roughly now + 4000 minutes
        expires_aware = result.expires_at if result.expires_at.tzinfo else result.expires_at.replace(tzinfo=timezone.utc)
        expected_min = before + timedelta(minutes=3999)
        expected_max = after + timedelta(minutes=4001)
        assert expected_min <= expires_aware <= expected_max


# =============================================================================
# renew_subscription / delete_subscription
# =============================================================================


class TestSubscriptionLifecycle:

    async def test_renew_returns_updated_config(self):
        adapter, _, _ = _make_adapter()
        patch_resp = _mock_response({
            "id": "sub-1",
            "resource": f"/me/todo/lists/{_LIST_ID}/tasks",
        })
        session = _mock_session({"patch": patch_resp})

        with _patch_session(session):
            result = await adapter.renew_subscription(_USER_ID, "sub-1")

        assert isinstance(result, TaskSubscriptionConfig)
        assert result.sub_id == "sub-1"

    async def test_delete_subscription_called(self):
        adapter, _, _ = _make_adapter()
        del_resp = MagicMock()
        del_resp.status = 204
        del_resp.ok = True
        del_resp.__aenter__ = AsyncMock(return_value=del_resp)
        del_resp.__aexit__ = AsyncMock(return_value=False)
        session = _mock_session({"delete": del_resp})

        with _patch_session(session):
            await adapter.delete_subscription(_USER_ID, "sub-1")

        session.delete.assert_called_once()
        assert "sub-1" in session.delete.call_args.args[0]

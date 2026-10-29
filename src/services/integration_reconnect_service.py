"""
IntegrationReconnectService — R1: Reconnect and one chat notice when a past connection has no credential
(docs/10_rfcs/SECRETS_AT_REST_RFC.md §6.2, F1, F2; plan D-P4, D-P5).

An integration whose own feature state shows a past connection (Gmail: an indexing state; To Do: a config with a primary
list; Drive: a sync state) but whose credential is gone shows Reconnect in the Cabinet and sends one notice per user and
integration (`reconnect_policy.r1_due`). R1 reads the feature stores, never the old credential collection, and lists its
users from them too: the daily-review and To Do renewal fan-outs list users from the credential store, which is empty
after the credential cutover.

`sweep()` is the hourly `/worker` task `reconnect_notice_sweep` for Gmail and To Do; Drive's notice runs in the drive
fan-out (`DriveSyncSchedulerService._reconnect_notice`), which already visits every sync state every 10 minutes. The
flag rules are the drive notice's (`drive_sync_scheduler_service.py`, "One notice per lost grant"): the flag on the
feature state goes up before the send and comes down again only when nothing can have been sent (no channel, a
formatter failure, `notify` raising); an `empty_answer` is final; a credential that is back lowers it.

`mark_disconnected` (the Cabinet's Gmail and Drive disconnects, F1) and `consented` (the three OAuth callbacks) keep the
mark; `status` answers whether the Cabinet shows Reconnect for an integration that reads "not connected".

Public interface:
    IntegrationReconnectService(credentials, users, notifier, *, email_states, task_config, drive_states, drive,
                                alert_sink, ensure_agents, clock)
    await sweep() -> Dict[str, int]
    await mark_disconnected(user_id, integration) -> None
    await consented(user_id, integration) -> None
    await status(user_id, integrations=ALL) -> Dict[str, bool]
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, Optional

from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..domain.reconnect_policy import R1_NOTICES, Cleared, Integration, r1_due, reconnect_needed
from ..ports.alert_sink import AlertSinkPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_config_port import TaskConfigPort
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService

GMAIL_PROVIDER = "gmail"
TODO_PROVIDER = "microsoft_todo"
ALL = tuple(Integration)

FlagWriter = Callable[[bool], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationReconnectService:
    def __init__(self, credentials: OAuthCredentialsPort, users: UserRepository,
                 notifier: Optional["UserNotificationService"], *,
                 email_states: Optional[IndexedEmailRepository] = None,
                 task_config: Optional[TaskConfigPort] = None,
                 drive_states: Optional[DriveIndexRepository] = None,
                 drive: Optional[UserDrivePort] = None,
                 alert_sink: Optional[AlertSinkPort] = None,
                 ensure_agents: Optional[Callable[[str], Awaitable[Any]]] = None,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._credentials = credentials
        self._users = users
        self._notifier = notifier
        self._email = email_states
        self._task_config = task_config
        self._drive_states = drive_states
        self._drive = drive
        self._alert_sink = alert_sink
        self._ensure_agents = ensure_agents
        self._clock = clock

    # -- the sweep (Gmail + To Do) --------------------------------------------------------------------------------

    async def sweep(self) -> Dict[str, int]:
        """One pass over every Gmail indexing state and every To Do config. A failure for one user and integration is
        logged and counted, never stops the rest."""
        summary = {"candidates": 0, "notices": 0, "errors": 0}
        passes = []
        if self._email is not None:
            passes.append((Integration.GMAIL, await self._email.list_indexing_user_ids(GMAIL_PROVIDER), self._gmail))
        if self._task_config is not None:
            passes.append((Integration.TODO, await self._task_config.list_user_ids(), self._todo))
        for integration, user_ids, visit in passes:
            for user_id in user_ids:
                summary["candidates"] += 1
                try:
                    summary["notices"] += await visit(user_id)
                except Exception as exc:
                    summary["errors"] += 1
                    logger.error(f"[Reconnect] {integration.value} check failed for {user_id[:8]}: {exc}",
                                 exc_info=True)
        logger.info(f"[Reconnect] sweep {summary}")
        return summary

    async def _gmail(self, user_id: str) -> int:
        email = self._email
        if email is None:   # the sweep visits Gmail only when the store is wired; its caller logs the raise
            raise RuntimeError("Gmail reconnect check without an indexing-state store")
        state = await email.get_indexing_state(user_id, GMAIL_PROVIDER)
        if state is None:
            return 0

        async def flag(value: bool) -> None:
            await email.set_reconnect_flags(user_id, GMAIL_PROVIDER, reconnect_notified=value)

        has_credential = await self._credentials.is_connected(user_id, GMAIL_PROVIDER)
        if has_credential:
            if state.reconnect_notified:
                await flag(False)
            return 0
        if not r1_due(has_credential, True, state.disconnected_by_user_at is not None, state.reconnect_notified):
            return 0
        return await self._notice(user_id, Integration.GMAIL, flag)

    async def _todo(self, user_id: str) -> int:
        task_config = self._task_config
        if task_config is None:   # the sweep visits To Do only when the store is wired; its caller logs the raise
            raise RuntimeError("To Do reconnect check without a task config store")
        config = await task_config.get_config(user_id)

        async def flag(value: bool) -> None:
            # a field write: never a config rewrite, which could undo a concurrent save of the subscriptions
            await task_config.set_reconnect_notified(user_id, value)

        has_credential = await self._credentials.is_connected(user_id, TODO_PROVIDER)
        if has_credential:
            if config.reconnect_notified:
                await flag(False)
            return 0
        # D-P5: a deliberate disconnect resets the config, so it needs no mark
        if not r1_due(has_credential, bool(config.primary_list_id), False, config.reconnect_notified):
            return 0
        return await self._notice(user_id, Integration.TODO, flag)

    async def _notice(self, user_id: str, integration: Integration, flag: FlagWriter) -> int:
        """Send the one notice under the flag (module docstring). Returns 1 when it was delivered."""
        name = integration.value
        if self._notifier is None:
            logger.warning(f"[Reconnect] {name} needs reconnect for {user_id[:8]}; no notifier configured")
            return 0
        profile = await self._users.get_user(user_id)
        if profile is None:
            logger.warning(f"[Reconnect] {name} needs reconnect for {user_id[:8]}, who has no user profile; skipped")
            return 0
        # The formatter agent is registered only while the user's agents are loaded. Before the flag: a failed load
        # leaves it down and the next sweep retries.
        if self._ensure_agents is not None:
            await self._ensure_agents(user_id)
        # Up BEFORE the send: a request cancelled after the message left must not repeat it.
        await flag(True)
        try:
            result = await self._notifier.notify(user_id=user_id, account_id=profile.account_id or "",
                                                 system_alert=R1_NOTICES[integration],
                                                 kind=NotificationKind.DOCUMENT_DELIVERY)
        except Exception as exc:   # notify raises only before any send (channel lookup)
            logger.error(f"[Reconnect] {name} notice for {user_id[:8]} raised ({exc}); retried on the next sweep")
            await flag(False)
            raise
        path = f"{name}_reconnect_notice"
        if is_empty_answer(result):
            logger.error(f"[Reconnect] background_empty_answer: {name} notice for {user_id[:8]}; not retried",
                         extra={"event": "background_empty_answer", "path": path, "user_id": user_id[:8]})
            if self._alert_sink is not None:
                try:
                    await self._alert_sink.post(empty_answer_alert(path, user_id))
                except Exception as exc:
                    logger.warning(f"[Reconnect] background_empty_answer alert failed: {exc}")
            return 0
        if not result.delivered and not result.sent:
            await flag(False)
            logger.warning(f"[Reconnect] {name} notice for {user_id[:8]} not sent ({result.agent_status.value}, "
                           f"{result.error}); retried on the next sweep")
            return 0
        if not result.delivered:
            logger.warning(f"[Reconnect] {name} notice for {user_id[:8]} sent but not confirmed delivered "
                           f"({result.agent_status.value}, {result.error}); not retried")
            return 0
        logger.info(f"[Reconnect] {name} notice sent to {user_id[:8]}")
        return 1

    # -- the Cabinet and the consent callbacks --------------------------------------------------------------------

    async def mark_disconnected(self, user_id: str, integration: Integration) -> None:
        """The user disconnected on purpose (F1): mark the feature state so R1 skips it. Gmail and Drive only; To Do's
        disconnect resets its config (D-P5). A store that is not wired, or a missing state, writes nothing."""
        now = self._clock()
        if integration is Integration.GMAIL:
            if self._email is not None:
                await self._email.set_reconnect_flags(user_id, GMAIL_PROVIDER, disconnected_by_user_at=now)
        elif integration is Integration.DRIVE:
            if self._drive_states is not None:
                await self._drive_states.update_sync_flags(user_id, disconnected_by_user_at=now)
        else:
            raise ValueError(f"{integration.value} has no disconnect mark (its disconnect resets its config)")
        logger.info(f"[Reconnect] {integration.value} marked disconnected by user for {user_id[:8]}")

    async def consented(self, user_id: str, integration: Integration) -> None:
        """A new consent stored a credential: clear the mark and the notice flag, so the next loss notifies again.
        Drive's flag is lowered by the drive fan-out once the grant works (its single clear path)."""
        if integration is Integration.GMAIL:
            if self._email is not None:
                await self._email.set_reconnect_flags(user_id, GMAIL_PROVIDER, reconnect_notified=False,
                                                      disconnected_by_user_at=Cleared.MARK)
        elif integration is Integration.DRIVE:
            if self._drive_states is not None:
                await self._drive_states.update_sync_flags(user_id, disconnected_by_user_at=Cleared.MARK)
        elif self._task_config is not None:
            config = await self._task_config.get_config(user_id)
            if config.reconnect_notified:
                await self._task_config.set_reconnect_notified(user_id, False)

    async def status(self, user_id: str, integrations: Iterable[Integration] = ALL) -> Dict[str, bool]:
        """Per integration: True when the Cabinet shows Reconnect (`reconnect_policy.reconnect_needed`)."""
        out: Dict[str, bool] = {}
        for integration in integrations:
            out[integration.value] = await self._needs_reconnect(user_id, integration)
        return out

    async def _needs_reconnect(self, user_id: str, integration: Integration) -> bool:
        if integration is Integration.GMAIL:
            if self._email is None:
                return False
            state = await self._email.get_indexing_state(user_id, GMAIL_PROVIDER)
            if state is None:
                return False
            return reconnect_needed(await self._credentials.is_connected(user_id, GMAIL_PROVIDER), True,
                                    state.disconnected_by_user_at is not None)
        if integration is Integration.DRIVE:
            if self._drive_states is None or self._drive is None:
                return False
            drive_state = await self._drive_states.get_sync_state(user_id)
            if drive_state is None:
                return False
            connection = await self._drive.connection(user_id)
            return reconnect_needed(connection.connected, True, drive_state.disconnected_by_user_at is not None)
        if self._task_config is None:
            return False
        config = await self._task_config.get_config(user_id)
        return reconnect_needed(await self._credentials.is_connected(user_id, TODO_PROVIDER),
                                bool(config.primary_list_id), False)

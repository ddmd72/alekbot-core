"""ExternalWorkerUserGate — the access whitelist of a worker-side call (EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6).

`ExternalTaskService.resolve_key` checks the key, the worker, the account lock and the flag; it leaves the access
whitelist out (the queue's core knows nothing of who may use the product). Every surface that takes a worker key runs
this gate on the resolved principal, so the REST endpoint and the MCP resource apply one rule:
- the stored user is read fresh (never a cached profile);
- a stored user that is inactive is `account_locked`;
- a user that is not stored, has no email or whose email the whitelist does not allow is `not_enabled`.

A read error propagates: the caller fails closed.
"""
from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_worker_principal import WorkerPrincipal
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository


class ExternalWorkerUserGate:

    def __init__(self, user_repo: UserRepository, whitelist_repo: WhitelistRepository) -> None:
        self._users = user_repo
        self._whitelist = whitelist_repo

    async def check(self, principal: WorkerPrincipal) -> None:
        """Return when the principal's user may use the queue now; raise `ExternalTaskRefused` otherwise."""
        user = await self._users.get_user(principal.user_id)
        if user is not None and not user.is_active:
            raise ExternalTaskRefused(TaskRefusal.ACCOUNT_LOCKED)
        if user is None or not user.email or not (await self._whitelist.get_whitelist()).is_allowed(user.email):
            raise ExternalTaskRefused(TaskRefusal.NOT_ENABLED)

"""ExternalWorkerUserGate: the whitelist rule every worker-key surface applies to a resolved principal
(EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6).

Alex Doe is `usr-1` (account `acc-1`) with the worker `w1`; the repositories are the MCP gate fakes.
"""
import pytest

from src.domain.external_task import SubmitVia
from src.domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from src.domain.external_worker_principal import WorkerPrincipal
from src.domain.user import InactiveReason, UserProfile
from src.services.external_worker_user_gate import ExternalWorkerUserGate
from tests.support.mcp_gate_fakes import ALLOWED_EMAIL, REMOVED_EMAIL, FakeUserRepo, FakeWhitelistRepo

PRINCIPAL = WorkerPrincipal(user_id="usr-1", account_id="acc-1", worker_id="w1", via=SubmitVia.KEY)


def _gate(email=ALLOWED_EMAIL, *, stored=True, active=True, whitelist=None):
    users = FakeUserRepo({"usr-1": email} if stored else {})
    if stored and not active:
        users.users["usr-1"] = UserProfile(user_id="usr-1", email=email, is_active=False,
                                           inactive_reason=InactiveReason.DURESS)
    whitelist = whitelist or FakeWhitelistRepo()
    return ExternalWorkerUserGate(users, whitelist), whitelist


async def _refusal(gate):
    with pytest.raises(ExternalTaskRefused) as refused:
        await gate.check(PRINCIPAL)
    return refused.value.reason


async def test_a_stored_active_whitelisted_user_passes():
    gate, _ = _gate()
    assert await gate.check(PRINCIPAL) is None


async def test_a_whitelisted_domain_passes():
    gate, _ = _gate(REMOVED_EMAIL, whitelist=FakeWhitelistRepo(emails=(), domains=("acme.example",)))
    assert await gate.check(PRINCIPAL) is None


async def test_a_locked_user_is_account_locked_before_the_whitelist_is_read():
    whitelist = FakeWhitelistRepo()
    whitelist.error = AssertionError("the whitelist must not be read for a locked user")
    gate, _ = _gate(active=False, whitelist=whitelist)
    assert await _refusal(gate) is TaskRefusal.ACCOUNT_LOCKED


async def test_a_locked_user_off_the_whitelist_is_still_account_locked():
    gate, _ = _gate(REMOVED_EMAIL, active=False)
    assert await _refusal(gate) is TaskRefusal.ACCOUNT_LOCKED


@pytest.mark.parametrize("email,stored", [(REMOVED_EMAIL, True), (None, True), ("", True), (ALLOWED_EMAIL, False)],
                         ids=["off_the_whitelist", "no_email", "empty_email", "not_stored"])
async def test_every_other_user_is_not_enabled(email, stored):
    gate, _ = _gate(email, stored=stored)
    assert await _refusal(gate) is TaskRefusal.NOT_ENABLED


async def test_a_whitelist_read_error_propagates():
    whitelist = FakeWhitelistRepo()
    whitelist.error = RuntimeError("whitelist store down")
    gate, _ = _gate(whitelist=whitelist)
    with pytest.raises(RuntimeError):
        await gate.check(PRINCIPAL)


async def test_each_check_reads_the_whitelist_fresh():
    gate, whitelist = _gate()
    await gate.check(PRINCIPAL)
    whitelist.entry = FakeWhitelistRepo(emails=()).entry     # removed between two calls
    assert await _refusal(gate) is TaskRefusal.NOT_ENABLED

"""billed_scope: the billing unit of background work outside an agent execution (decisions/billing_embeddings.md)."""
import pytest

from src.domain.billing import calculate_cost, current_ledger, ledger_scope
from src.ports.quota_service import QuotaService
from src.utils.billed_scope import billed_scope

EMBED = "gemini-embedding-2"


class RecordingQuota(QuotaService):
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def record_usage(self, account_id, model, tokens, cost):
        if self.fail:
            raise RuntimeError("store down")
        self.calls.append((account_id, model, tokens, cost))


def test_the_embedding_model_is_priced():
    assert calculate_cost(EMBED, 1_000_000, 0) == 0.20


async def test_one_write_per_model_leg_priced_by_the_model():
    quota = RecordingQuota()
    async with billed_scope(quota, "account-a") as ledger:
        ledger.add(EMBED, prompt_tokens=400_000)
        ledger.add(EMBED, prompt_tokens=600_000)
        ledger.add("gemini-flash-latest", prompt_tokens=1000, completion_tokens=100)
    assert quota.calls == [
        ("account-a", EMBED, 1_000_000, 0.20),
        ("account-a", "gemini-flash-latest", 1100, calculate_cost("gemini-flash-latest", 1000, 100)),
    ]
    assert current_ledger() is None


async def test_it_flushes_when_the_block_raises():
    quota = RecordingQuota()
    with pytest.raises(ValueError):
        async with billed_scope(quota, "account-a") as ledger:
            ledger.add(EMBED, prompt_tokens=10)
            raise ValueError("boom")
    assert quota.calls == [("account-a", EMBED, 10, calculate_cost(EMBED, 10, 0))]


async def test_an_empty_scope_writes_nothing():
    quota = RecordingQuota()
    async with billed_scope(quota, "account-a"):
        pass
    assert quota.calls == []


async def test_no_quota_is_a_no_op_flush():
    async with billed_scope(None, "account-a") as ledger:
        ledger.add(EMBED, prompt_tokens=10)


@pytest.mark.parametrize("account", [None, ""])
async def test_no_account_writes_nothing_and_warns(account, caplog):
    quota = RecordingQuota()
    async with billed_scope(quota, account, "unit x") as ledger:
        ledger.add(EMBED, prompt_tokens=10)
    assert quota.calls == []
    assert any("unit x" in r.getMessage() and "no account" in r.getMessage() for r in caplog.records)


async def test_a_failing_write_is_logged_not_raised(caplog):
    quota = RecordingQuota(fail=True)
    async with billed_scope(quota, "account-a") as ledger:
        ledger.add(EMBED, prompt_tokens=10)
    assert any("billing write failed" in r.getMessage() for r in caplog.records)


async def test_an_inner_scope_bills_its_own_tokens_and_the_outer_the_rest():
    inner_quota, outer_quota = RecordingQuota(), RecordingQuota()
    with ledger_scope("account-a") as outer:
        outer.add("gemini-flash-latest", prompt_tokens=100)
        async with billed_scope(inner_quota, "account-a") as inner:
            assert current_ledger() is inner
            inner.add(EMBED, prompt_tokens=40)
        assert current_ledger() is outer
        outer.add("gemini-flash-latest", prompt_tokens=50)
    assert inner_quota.calls == [("account-a", EMBED, 40, calculate_cost(EMBED, 40, 0))]
    assert outer.by_model["gemini-flash-latest"].prompt_tokens == 150
    assert EMBED not in outer.by_model
    assert outer_quota.calls == []


async def test_usage_with_no_quota_service_warns_once_a_minute_per_site(caplog, monkeypatch):
    from src.utils import billed_scope as mod
    monkeypatch.setattr(mod, "_last_no_quota_log", {})
    for _ in range(2):
        async with billed_scope(None, "account-a", "site one") as ledger:
            ledger.add(EMBED, prompt_tokens=10)
    async with billed_scope(None, "account-a", "site two") as ledger:
        ledger.add(EMBED, prompt_tokens=10)
    async with billed_scope(None, "account-a", "site one"):
        pass   # nothing spent: nothing to say
    lines = [r.getMessage() for r in caplog.records if "no quota service wired" in r.getMessage()]
    assert len(lines) == 2 and "site one" in lines[0] and "site two" in lines[1]


async def test_a_zero_token_leg_is_not_written():
    """`TokenLedger.add` creates the model's leg even for zero usage (a call that reported none), so a ledger can hold
    one; writing it would add an empty record."""
    quota = RecordingQuota()
    async with billed_scope(quota, "account-a") as ledger:
        ledger.add("gemini-flash-latest")
        ledger.add(EMBED, prompt_tokens=10)
        assert ledger.by_model["gemini-flash-latest"].total_tokens == 0
    assert quota.calls == [("account-a", EMBED, 10, calculate_cost(EMBED, 10, 0))]

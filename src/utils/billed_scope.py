"""Billing scope for background work that makes billed calls outside an agent execution (embeddings, mostly).

An agent execution bills through its own ledger (``BaseAgent._flush_billing``). Indexers, the fact profile step and
the Cabinet routes embed text with no agent around them, so nothing counted that spend. ``billed_scope`` opens a
``TokenLedger`` for one natural unit of work (an indexing tick, a page, a request) and writes it to the account's
daily counter once per model leg when the unit ends. One scope per unit, never per call: the write count stays small.

A scope opened inside an agent execution REPLACES the execution's ledger for its block and resets after, so the inner
scope bills its own tokens and the agent bills the rest, nothing twice.

Best-effort like the agent path: it never raises and never gates the work. The flush is awaited before the unit
returns, because Cloud Run throttles CPU once a response is sent and a detached write would be lost.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from ..domain.billing import TokenLedger, ledger_scope
from ..ports.quota_service import QuotaService
from ..utils.logger import logger


@asynccontextmanager
async def billed_scope(
    quota: Optional[QuotaService], account_id: Optional[str], unit: str = ""
) -> AsyncIterator[TokenLedger]:
    """Collect the billed usage of the block and record it on ``account_id`` when the block ends (also on error).

    No-op flush without a quota service; with usage but no account it logs a warning (the spend is unattributable).
    ``unit`` only labels the log lines.
    """
    with ledger_scope(account_id) as ledger:
        try:
            yield ledger
        finally:
            await _flush(quota, ledger, unit)


async def _flush(quota: Optional[QuotaService], ledger: TokenLedger, unit: str) -> None:
    try:
        if quota is None or ledger.is_empty:
            return
        if not ledger.account_id:
            logger.warning(f"[BilledScope] {unit or 'unit'}: {ledger.total_tokens} tokens with no account; not recorded")
            return
        for model, leg in ledger.by_model.items():
            if leg.total_tokens == 0:
                continue
            await quota.record_usage(
                account_id=ledger.account_id, model=model, tokens=leg.total_tokens, cost=leg.cost(model)
            )
    except Exception as exc:
        logger.error(f"[BilledScope] {unit or 'unit'}: billing write failed: {type(exc).__name__}: {exc}")

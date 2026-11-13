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
import time
from typing import AsyncIterator, Dict, Optional

from ..domain.billing import TokenLedger, ledger_scope
from ..ports.quota_service import QuotaService
from ..utils.logger import logger

_NO_QUOTA_LOG_INTERVAL_S = 60.0
_last_no_quota_log: Dict[str, float] = {}   # by unit label; touched only from the event-loop thread


@asynccontextmanager
async def billed_scope(
    quota: Optional[QuotaService], account_id: Optional[str], unit: str = ""
) -> AsyncIterator[TokenLedger]:
    """Collect the billed usage of the block and record it on ``account_id`` when the block ends (also on error).

    Without a quota service the usage is not recorded (a WARNING at most once a minute per unit); with usage but no account it logs a warning (the spend is unattributable).
    ``unit`` only labels the log lines.
    """
    with ledger_scope(account_id) as ledger:
        try:
            yield ledger
        finally:
            await _flush(quota, ledger, unit)


async def _flush(quota: Optional[QuotaService], ledger: TokenLedger, unit: str) -> None:
    try:
        if ledger.is_empty:
            return
        if quota is None:
            # A site wired without a quota service loses its spend; say so, once a minute per site
            now = time.monotonic()
            if now - _last_no_quota_log.get(unit, -_NO_QUOTA_LOG_INTERVAL_S) >= _NO_QUOTA_LOG_INTERVAL_S:
                _last_no_quota_log[unit] = now
                logger.warning(f"[BilledScope] {unit or 'unit'}: {ledger.total_tokens} tokens with no quota service wired; not recorded")
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

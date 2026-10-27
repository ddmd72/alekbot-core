"""The older consolidation benches' dry run (pre-S3 kit): put a bench's dry-run wrapper under both of the agent's write ports.

`ConsolidationAgent` has two: the model's (`_fact_management`) and the typed path (`_fact_updates`); production fills
both with the one adapter. A bench that swaps only the first leaves the typed path on the live store. These wrappers
offer the model's path only, so the typed path gets `NoTypedWrites`, which refuses: such a bench cannot run the review
pass. The S3 kit (`s3_dry_run_kit.py`) records both paths and is the way to dry-run the review.
"""

from datetime import datetime
from typing import Any, Optional

from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.domain.fact_write_context import FactWriteContext
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_update_port import FactUpdatePort


class NoTypedWrites(FactUpdatePort):
    """The typed write path of a bench whose dry run has none: every typed write raises, nothing reaches a store."""

    async def apply_fact_update(
        self,
        fact_id: str,
        update: ParsedFactUpdate,
        account_id: str,
        *,
        context: FactWriteContext,
        evidence_at: Optional[datetime] = None,
        reason: Optional[str] = None,
        history: Optional[str] = None,
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        raise RuntimeError(
            "this bench's dry run has no typed write path (the review pass): run it on the S3 kit (--s3-kit)"
        )


def install_dry_run(agent: Any, dry_run: FactManagementPort) -> None:
    """Swap `agent`'s model port for `dry_run` and its typed port for `dry_run` too when it has the typed path, else for
    `NoTypedWrites`: no write of either kind reaches the live store."""
    agent._fact_management = dry_run
    agent._fact_updates = dry_run if isinstance(dry_run, FactUpdatePort) else NoTypedWrites()

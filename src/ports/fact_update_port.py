"""FactUpdatePort — code's write of one fact update, typed: the review's decisions and the owner's answers.

Why a port of its own and not a fifth method of `FactManagementPort`: that port is the consolidation model's tool surface
(model JSON in, results the model reads), and its every implementer (the tracker, the dry-run wrappers, the test fakes)
is a model tool wrapper. This one takes an update code has already decided (`ParsedFactUpdate`) and the inputs only code
has: the evidence moment, the code-built trail reason and a history fact's text. `FactManagementAdapter` implements both
with one apply, so the model's `update_fact` is `parse_fact_update` + this write.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional

from ..domain.fact_update import ParsedFactUpdate
from ..domain.fact_write_context import FactWriteContext


class FactUpdatePort(ABC):
    @abstractmethod
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
    ) -> Dict[str, Any]:
        """Apply `update` to one fact under the write rules of `FactManagementPort.update_fact` (versions, in place,
        resolution through `context.accepts`, the conflict retry, tenancy), which this method is the second entry to.

        Args:
            fact_id: any id of the fact's chain; a superseded id is followed to the current version.
            update: the decided update; `notes` are carried into the result's `note`.
            account_id: the caller's account.
            context: the pass the write belongs to; its actor is written to the trail.
            evidence_at: the moment the review's evidence or the owner's answer is from; it may move a revive's
                reported_date (domain.fact_update_lifecycle.finish_fact_update) and is the history fact's
                reported_date. Other passes ignore it.
            reason: the code-built trail reason of a review or owner write (the builders of
                domain.fact_review_reasons); finish_fact_update requires it for them.
            history: the text of a fact to create beside an archive (domain.fact_review_answers.history_refusal says
                when a call may carry it; history_fact and history_trail build the fact). The archive and the new fact
                are one transaction (FactRepository.archive_and_add).

        Returns: the result dict of `update_fact` (domain.fact_tool_result words). With `history`, `fact_id` is the
            archived fact and "history_fact_id" (domain.fact_tool_result.HISTORY_FACT_ID) the new one.
        Errors: never raises. A write the domain refuses is "failed" with nothing written; its `message` is the
            refusal as the domain states it (for the caller's log, not a model's instructions).
        """

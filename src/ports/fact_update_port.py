"""FactUpdatePort — code's write of one fact update, typed: an update code has already decided (`ParsedFactUpdate`),
with the inputs only code has (the evidence moment, the code-built trail reason, a history fact's text). The model's
`FactManagementPort.update_fact` parses its arguments to the same type and runs the same write rules.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..domain.fact_update import ParsedFactUpdate
from ..domain.fact_update_outcome import FactUpdateOutcome
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
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        """Apply `update` to one fact under the write rules of `FactManagementPort.update_fact` (versions, in place,
        resolution through `context.accepts`, the conflict retry, tenancy), which this method is the second entry to.

        Args:
            fact_id: any id of the fact's chain; a superseded id is followed to the current version.
            update: the decided update; its `notes` are carried into the outcome's `notes`. It is held to the rules the
                model's arguments are parsed under (domain.fact_update.typed_update_refusal) before anything is read.
            account_id: the caller's account.
            context: the pass the write belongs to; its actor is written to the trail.
            evidence_at: the moment the review's evidence or the owner's answer is from; it may move a revive's
                reported_date (domain.fact_update_lifecycle.finish_fact_update) and is the history fact's
                reported_date. Other passes ignore it.
            reason: the code-built trail reason of a review or owner write (the builders of
                domain.fact_review_reasons); finish_fact_update requires it for them.
            expected_id: the exact document the caller decided on (the one it read). When the first resolution of `fact_id`
                is another document, the write is refused MOVED and nothing is written; None follows the chain forward.
            history: the text of a fact to create beside an archive (domain.fact_review_answers.history_refusal says
                when a call may carry it; history_fact and history_trail build the fact). The archive and the new fact
                are one transaction (FactRepository.archive_and_add).

        Returns: the typed outcome (domain.fact_update_outcome.FactUpdateOutcome): the document written, its version,
            the current document the update was applied to and the notes; with `history`, the archived fact is the one
            written and `history_fact_id` the new one. `update_fact`'s dict is this outcome rendered
            (domain.fact_tool_result.update_result).
        Errors: never raises. A write that is refused or fails has `refusal` (domain.fact_update_outcome.UpdateRefusal,
            e.g. CONFLICT after the retry, EXPIRY, BORN_DUE) and nothing written; its `refusal_text` is the refusal as the
            domain states it (for the caller's log, not a model's instructions).
        """

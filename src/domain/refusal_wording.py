"""Which text a date refusal gets (Facts S4 T2a). The texts need the dates and the zone of the refusal, so they are built
where the refusal is; a pass's row in `PassActor` holds this choice."""

from enum import StrEnum


class RefusalWording(StrEnum):
    STAGE1 = "stage1"      # a candidate: the model may DISCARD it
    CLEANUP = "cleanup"    # stored memory, S3-D12: rewrite it as what happened, or give a future date
    REVIEW = "review"      # stored memory brought back by the review or the owner: give a horizon, or archive

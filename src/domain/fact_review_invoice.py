"""A routine invoice the review rewrote as history "payment not confirmed" (P2-2): what code turns into a digest question
after the pass, so the owner can tick it paid or not paid in the Cabinet."""
from dataclasses import dataclass


@dataclass(frozen=True)
class InvoiceRequest:
    """`fact_id`: the batch fact; `written_id`: the history version the pass wrote; `paid` and `not_paid`: the texts each
    answer writes as history over it (the `payment` argument, checked by `payment_refusal`)."""

    fact_id: str
    written_id: str
    paid: str
    not_paid: str

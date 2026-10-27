"""Invented outcome texts of P2 questions (a concert, a gym membership, a parking fine), shared by the P2 domain tests."""

HAPPENED_TEXTS = {
    "happened": "The user went to the concert on Dec 5, 2026.",
    "not_happened": "The user planned to go to the concert on Dec 5, 2026, and did not go.",
    "unknown": "The user planned to go to the concert on Dec 5, 2026; whether it took place is unknown.",
}
RENEWED_TEXTS = {
    "renewed": "The user has a gym membership, renewed in Nov 2026.",
    "not_renewed": "The user's gym membership ended in Nov 2026 and was not renewed.",
    "not_needed": "The user had a gym membership until Nov 2026; it is no longer needed.",
    "unknown": "The user had a gym membership until Nov 2026; whether it was renewed is unknown.",
}
PAID_TEXTS = {
    "paid": "The user paid a parking fine of 80 EUR, due Nov 30, 2026.",
    "not_paid": "The user did not pay a parking fine of 80 EUR, due Nov 30, 2026.",
    "unknown": "The user had a parking fine of 80 EUR, due Nov 30, 2026; payment not confirmed.",
}
DIGEST_TEXTS = {k: PAID_TEXTS[k] for k in ("paid", "not_paid")}
TEXTS = {"still_true": {}, "happened": HAPPENED_TEXTS, "renewed": RENEWED_TEXTS, "paid": PAID_TEXTS}

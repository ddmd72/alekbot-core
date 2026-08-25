from datetime import datetime, timezone

from src.domain.companion import CompanionRecord


def test_minimal_construction_generates_id_and_created_at():
    record = CompanionRecord(
        session_id="user1:C123",
        account_id="acc1",
        created_by_user_id="user1",
        text="Recurring subjunctive error on hypothetical clauses",
        domain="grammar_error",
    )
    assert record.id  # uuid4, non-empty
    assert isinstance(record.created_at, datetime)
    assert record.vector is None
    assert record.tags == []


def test_no_scd2_fields():
    """Regression guard: RFC §6 — no lineage_id/valid_from/valid_to/is_current."""
    fields = CompanionRecord.model_fields
    for scd2_field in ("lineage_id", "valid_from", "valid_to", "is_current"):
        assert scd2_field not in fields


def test_explicit_id_and_vector():
    record = CompanionRecord(
        id="explicit-id",
        session_id="user1:C123",
        account_id="acc1",
        created_by_user_id="user1",
        text="text",
        vector=[0.1, 0.2, 0.3],
        tags=["grammar", "subjunctive"],
        domain="grammar_error",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    assert record.id == "explicit-id"
    assert record.vector == [0.1, 0.2, 0.3]
    assert record.tags == ["grammar", "subjunctive"]

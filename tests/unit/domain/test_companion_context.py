from src.domain.companion import CompanionRecord
from src.domain.companion_context import CompanionContext
from src.domain.entities import FactDomain, FactEntity, FactType
from src.domain.search import EnrichedFact


def test_defaults_are_empty():
    ctx = CompanionContext()
    assert ctx.session_summary is None
    assert ctx.own_records == []
    assert ctx.biographical_facts == []
    assert ctx.standing_directives == []


def test_construction_with_all_fields():
    record = CompanionRecord(
        session_id="slack:C1", account_id="acc1", created_by_user_id="u1",
        text="Recurring subjunctive error", domain="grammar_error",
    )
    fact = EnrichedFact(fact_id="f1", content="likes spanish soap operas", source="phrase1_text")
    directive = FactEntity(
        account_id="acc1", created_by_user_id="acc1", lineage_id="l1",
        text="Never give partial answers", type=FactType.STATE,
        domain=FactDomain.AGENT_DIRECTIVE,
    )

    ctx = CompanionContext(
        session_summary="Two weeks in, mixing preterite/imperfect",
        own_records=[record],
        biographical_facts=[fact],
        standing_directives=[directive],
    )

    assert ctx.session_summary == "Two weeks in, mixing preterite/imperfect"
    assert ctx.own_records == [record]
    assert ctx.biographical_facts == [fact]
    assert ctx.standing_directives == [directive]

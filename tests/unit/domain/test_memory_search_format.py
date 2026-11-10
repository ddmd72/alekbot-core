"""The shared memory search formatter (connector and memory agent): header, tags, dates, order. Invented universe."""
from datetime import datetime, timezone

from src.domain.entities import FactEntity, FactType
from src.domain.fact_search_result import PhraseSearchResult
from src.domain.memory_search_format import FACT_SEPARATOR, RESULT_EXPLANATION, format_search_result
from src.domain.memory_search_texts import NO_RECORDS_TEXT, QUERIES_FIELD_TEXT


def _fact(text, *, day=(2026, 3, 1), points=None, primary="travel", **kw) -> FactEntity:
    return FactEntity(
        id=f"f-{text[:6]}", account_id="acc-1", created_by_user_id="usr-1", lineage_id="l", text=text,
        type=FactType.EVENT, primary_domain=primary, time_points=points,
        reported_date=datetime(*day, 9, 30, tzinfo=timezone.utc), **kw)


COIMBRA = _fact("Weekend in Coimbra with Clara Mar 6-8", points=[{"from": "2027-03-06", "to": "2027-03-08"}],
                context="Planned with Clara", metadata={"source": "chat"})
BIRTHDAY = _fact("Clara's birthday is on 07-14", day=(2026, 5, 20), primary="family")
SERVICE = _fact("Car service 2026-11-05", day=(2026, 11, 6), primary="vehicles")
PHRASES = ["Weekend in Coimbra", "Clara's birthday", "Jamie's school meeting"]


def _render():
    return format_search_result(PHRASES, PhraseSearchResult((COIMBRA, BIRTHDAY, SERVICE), ((0, 1), (1,), (0,))))


def test_the_header_counts_the_facts_each_phrase_found_and_shows_a_zero():
    header = _render().split("\n\n")[0].splitlines()
    assert header[0] == RESULT_EXPLANATION
    assert header[1:] == ['q1 "Weekend in Coimbra": 2 found', 'q2 "Clara\'s birthday": 2 found',
                          'q3 "Jamie\'s school meeting": 0 found']


def test_a_fact_block_has_tags_domain_when_and_the_recorded_date_then_text_context_metadata():
    first = _render().split("\n\n", 1)[1].split(FACT_SEPARATOR)[0].splitlines()
    assert first == ["[q1,q2] | Travel | when: 2027-03-06–2027-03-08 | recorded 2026-03-01",
                     "Weekend in Coimbra with Clara Mar 6-8", "context: Planned with Clara",
                     'metadata: {"source": "chat"}']


def test_a_fact_without_time_points_has_no_when_part_and_keeps_its_own_date():
    second = _render().split(FACT_SEPARATOR)[1].splitlines()
    assert second[0] == "[q2] | Family | recorded 2026-05-20"
    assert "when" not in second[0] and len(second) == 2


def test_the_fused_order_is_kept_with_no_domain_grouping():
    blocks = _render().split("\n\n", 1)[1].split(FACT_SEPARATOR)
    assert [b.splitlines()[1] for b in blocks] == [COIMBRA.text, BIRTHDAY.text, SERVICE.text]


def test_no_facts_is_the_no_records_text():
    assert format_search_result(PHRASES, PhraseSearchResult((), ())) == NO_RECORDS_TEXT


def test_the_queries_field_text_tells_the_caller_to_say_the_user_not_a_name():
    assert 'Refer to the user as "the user", never by their name' in QUERIES_FIELD_TEXT

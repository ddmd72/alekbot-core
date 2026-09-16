from dataclasses import dataclass
from src.domain.rrf import apply_rrf_ranking


@dataclass
class _Item:
    key: str
    label: str


def test_empty_input_returns_empty():
    assert apply_rrf_ranking([], key_fn=lambda i: i.key) == []


def test_single_query_preserves_order():
    items = [_Item("a", "A"), _Item("b", "B")]
    result = apply_rrf_ranking([items], key_fn=lambda i: i.key)
    assert [i.key for i in result] == ["a", "b"]


def test_shared_item_ranks_above_unique():
    shared = _Item("s", "shared")
    unique = _Item("u", "unique")
    result = apply_rrf_ranking([[shared, unique], [shared]], key_fn=lambda i: i.key)
    assert [i.key for i in result] == ["s", "u"]


def test_dedupes_by_key_across_queries():
    item = _Item("x", "X")
    result = apply_rrf_ranking([[item], [item], [item]], key_fn=lambda i: i.key)
    assert len(result) == 1


def test_custom_key_fn_used_not_hardcoded_attribute():
    """Regression guard: the function must not assume `.fact_id` — CompanionRecord uses `.id`."""
    @dataclass
    class _Other:
        id: str

    result = apply_rrf_ranking([[_Other(id="z")]], key_fn=lambda o: o.id)
    assert result[0].id == "z"

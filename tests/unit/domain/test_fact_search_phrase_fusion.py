"""Fusion of several phrases (fact profile RFC §5.3 "Several phrases", OD-14): each phrase's scores are read relative to
that phrase's own cut line and weighted by the phrase's position; a fact takes its best phrase, with no bonus for being
found twice; at most four phrases."""
from unittest.mock import AsyncMock

import pytest

from src.domain.fact_search_rank import PRIORITY_WEIGHTS, fuse_phrases
from src.ports.embedding_service import EmbeddingService
from src.ports.repository import FactRepository
from src.services.fact_search_service import FactSearchService
from tests.support.fact_search_kit import query
from tests.support.person_card_fakes import HeldCards


def test_one_phrase_keeps_its_score_order():
    phrase = [("knee", 3.0), ("dentist", 2.5), ("car", -1.0)]
    assert fuse_phrases([phrase], [2.5]) == ["knee", "dentist", "car"]


def test_a_tie_goes_to_the_smaller_id():
    assert fuse_phrases([[("lunch", 1.0), ("car", 1.0)]], [0.0]) == ["car", "lunch"]


def test_a_second_phrase_fact_well_above_its_line_beats_a_first_phrase_fact_well_below_its_line():
    first = [("coimbra", 9.0), ("hamburg", 1.0)]          # line at 8: hamburg lies far below it
    second = [("birthday", 12.0), ("lunch", 6.0)]         # line at 6: birthday lies far above it
    fused = fuse_phrases([first, second], [8.0, 6.0])
    assert fused.index("birthday") < fused.index("hamburg")
    assert fused[0] == "coimbra"


def test_a_raw_score_level_does_not_carry_across_phrases():
    first = [("coimbra", 100.0)]
    second = [("birthday", 2.0)]
    assert fuse_phrases([first, second], [100.0, 0.0]) == ["birthday", "coimbra"]


def test_a_fact_found_by_two_phrases_takes_its_best_value_and_no_bonus():
    first = [("a-single", 5.0), ("b-twice", 5.0)]
    second = [("b-twice", 4.0)]
    assert fuse_phrases([first, second], [5.0, 5.0]) == ["a-single", "b-twice"]
    better = [("b-twice", 9.0)]
    assert fuse_phrases([first, better], [5.0, 5.0])[0] == "b-twice"


def test_phrases_at_their_lines_rank_by_position():
    phrases = [[(f"p{i}", 0.0)] for i in reversed(range(len(PRIORITY_WEIGHTS)))]
    assert fuse_phrases(phrases, [0.0] * len(phrases)) == [f"p{i}" for i in reversed(range(len(PRIORITY_WEIGHTS)))]
    assert list(PRIORITY_WEIGHTS) == sorted(PRIORITY_WEIGHTS, reverse=True)


def test_more_phrases_than_priority_weights_or_a_missing_anchor_is_an_error():
    with pytest.raises(ValueError):
        fuse_phrases([[("a", 0.0)]] * (len(PRIORITY_WEIGHTS) + 1), [0.0] * (len(PRIORITY_WEIGHTS) + 1))
    with pytest.raises(ValueError):
        fuse_phrases([[("a", 0.0)], [("b", 0.0)]], [0.0])


async def test_the_port_refuses_more_than_four_queries_and_none_before_any_io():
    repository, embedder = AsyncMock(spec=FactRepository), AsyncMock(spec=EmbeddingService)
    cards = HeldCards()
    service = FactSearchService(repository, embedder, lambda: None, cards)
    with pytest.raises(ValueError):
        await service.search_phrases("acc-1", [query(subject=f"phrase {i}") for i in range(len(PRIORITY_WEIGHTS) + 1)])
    with pytest.raises(ValueError):
        await service.search_phrases("acc-1", [])
    assert repository.mock_calls == [] and embedder.mock_calls == [] and cards.reads == []

"""The outcome of a multi-phrase fact search: the facts in fused order and, per fact, which phrases found it."""
from dataclasses import dataclass
from typing import Tuple

from .entities import FactEntity


@dataclass(frozen=True)
class PhraseSearchResult:
    """`facts` in fused order; `phrase_matches[i]` holds the zero-based indices of the phrases whose own kept top list
    holds `facts[i]` (ascending). The two tuples have the same length."""
    facts: Tuple[FactEntity, ...]
    phrase_matches: Tuple[Tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if len(self.facts) != len(self.phrase_matches):
            raise ValueError("facts and phrase_matches differ in length")

"""How the memory search result is shown to the calling model: one formatter for the connector and the memory agent.

The caller sends one to four phrases, one aspect each. The header says how many returned facts each phrase found, so an
aspect that came back empty can be asked again; a fact's tags say which phrases found it; its two dates let the reader
spot a stale fact. Scores are not shown: they are not comparable across phrases.
"""
import json
from typing import List, Sequence

from . import fact_domains
from .entities import FactEntity
from .fact_search_result import PhraseSearchResult
from .fact_when import when_line
from .memory_search_texts import NO_RECORDS_TEXT

FACT_SEPARATOR = "\n---\n"

RESULT_EXPLANATION = (
    "Each q-number below is one of the phrases that were sent, with the count of returned records it found; the tag "
    "[q1,q3] on a record lists the phrases that found it, and a record with no tag came in through the fused ranking "
    "alone. Records are in relevance order. \"when\" is the time the record is about; \"recorded\" is the day it was "
    "stored, which may be long after the event. "
    "Some records match a phrase not by its topic but by one of its other sides — the same time, the same place, the "
    "same people or the same area of life: they show what else surrounds the matter in the user's life, and an answer "
    "that notices how they bear on it is worth more than one that sets them aside."
)


def _domain_label(fact: FactEntity) -> str:
    primary = getattr(fact, "primary_domain", None)
    group = (fact_domains.label(primary, "en") if primary else None) or getattr(fact, "domain", None) or "general"
    return str(getattr(group, "value", group))


def _fact_block(fact: FactEntity, matches: Sequence[int]) -> str:
    head: List[str] = []
    if matches:
        head.append("[" + ",".join(f"q{i + 1}" for i in matches) + "]")
    head.append(_domain_label(fact))
    when = when_line(fact)
    if when:
        head.append(when)
    if fact.reported_date:
        head.append(f"recorded {str(fact.reported_date)[:10]}")
    lines = [" | ".join(head), fact.text]
    if fact.context:
        lines.append(f"context: {fact.context}")
    if fact.metadata:
        lines.append(f"metadata: {json.dumps(fact.metadata, ensure_ascii=False)}")
    return "\n".join(lines)


def format_search_result(phrases: Sequence[str], result: PhraseSearchResult) -> str:
    """The explanation line, one header line per phrase, then one block per fact in fused order; `NO_RECORDS_TEXT`
    when nothing was found."""
    if not result.facts:
        return NO_RECORDS_TEXT
    header = [
        f'q{n + 1} "{phrase}": {sum(1 for m in result.phrase_matches if n in m)} found'
        for n, phrase in enumerate(phrases)
    ]
    blocks = [_fact_block(f, m) for f, m in zip(result.facts, result.phrase_matches)]
    return "\n".join([RESULT_EXPLANATION, *header]) + "\n\n" + FACT_SEPARATOR.join(blocks)

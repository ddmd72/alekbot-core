"""
Vocabulary-miss measurement, the pure half (USER_DRIVE_INDEXING_RFC §26.6, Δ66): no I/O, no clock, no logging.

`scripts/validation/drive_query_vocabulary.py` reads the recorded drive tool calls (Smart's before the drive
specialist, the specialist's after it: DRIVE_SPECIALIST_RFC §10 step 5) and the user's index; everything it
decides is decided here, so it is unit-testable on a synthetic sample. Several small public dataclasses live in this
one module, by decision: one vocabulary, read together (the same waiver as `src/domain/drive_match.py`).

Public interface:
    extract_events(rows) -> [(trace_id, Event)]     the searches and opens: Smart's `search_files_in_drive` /
                                                    `open_file_from_drive` delegations, the specialist's
                                                    `search_drive` / `open_drive_file` tool calls
    pair_searches(rows) -> [SearchRecord]           each search with the files opened after it in the same trace
    concepts_of(drive_query) -> (concepts, name_only, ignored_count, cut_count)
    probe_plan(concept, name_only) -> [ProbeGroup]  which keys to ask the index for, per stream and key family
    status_of(held_classes) -> STATUS_*              how a word is held across the index
    WordTally, tally_word(...)                       per-word counts over a window
    open_verdict(item_id, file, concepts) -> OpenVerdict      was the opened file in an exact tier; if not, its nearest keys
    render_report(...) -> str                        the markdown report
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_index_params import CONCEPT_KEYS_MAX
from src.domain.drive_keys import (
    fetch_batches, name_word_concepts, query_concepts, source_class_of_about_key, source_class_of_name_key,
    source_class_of_word_key,
)
from src.domain.drive_match import Concept, SourceClass
from src.domain.drive_tiers import CLASS_RANK, Cover, FileKeys, chunk_cover, record_cover
from scripts.validation.drive_eval.eval_connection import ref_item_id

INTENT_SEARCH = "search_files_in_drive"   # Smart's delegations, recorded before the drive specialist
INTENT_OPEN = "open_file_from_drive"
DELEGATE_TOOL = "delegate_to_specialist"
TOOL_SEARCH = "search_drive"              # the drive specialist's own tools (DRIVE_SPECIALIST_RFC §8.1)
TOOL_OPEN = "open_drive_file"

STREAM_ABOUT = "about"
STREAM_NAME = "name"
STREAM_CHUNK = "chunk"
STREAM_HINT = "hint"

STATUS_ABSENT = "absent"                  # df = 0: no file holds any key of the word
STATUS_AUTOMATIC = "automatic_only"       # held only by an automatic-record class
STATUS_TRANSLATION = "translation_only"   # held only by a translation class (skeletons, hint keys, unprinted forms)
STATUS_EXACT = "exact"                    # held by a verified-record or passage-words key
STATUSES = (STATUS_ABSENT, STATUS_AUTOMATIC, STATUS_TRANSLATION, STATUS_EXACT)

_EXACT_CLASSES = frozenset({SourceClass.VERIFIED_RECORD, SourceClass.PASSAGE_WORDS})
_EXACT_RANK = max(CLASS_RANK[c] for c in _EXACT_CLASSES)


# -- the recorded calls ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """One delegated call that matters here, in the order Smart made it."""
    kind: str                # INTENT_SEARCH | INTENT_OPEN
    drive_query: str = ""    # a search's query
    item_id: str = ""        # an open's item id (from its `drive:` ref)


@dataclass(frozen=True)
class SearchRecord:
    """One search and the distinct files opened after it, before Smart's next search in the same trace."""
    trace_id: str
    drive_query: str
    opened: Tuple[str, ...]


def _context_of(args: Mapping[str, Any]) -> Dict[str, Any]:
    raw = args.get("context")
    return dict(raw) if isinstance(raw, dict) else {}


def _events_of_row(tool_calls: Any) -> List[Event]:
    if not tool_calls:
        return []
    try:
        calls = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
    except ValueError:
        return []
    out: List[Event] = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            continue
        if call.get("name") == TOOL_SEARCH:
            out.append(Event(INTENT_SEARCH, drive_query=str(args.get("query") or "").strip()))
            continue
        if call.get("name") == TOOL_OPEN:
            item_id = ref_item_id(str(args.get("file_ref") or ""))
            if item_id:
                out.append(Event(INTENT_OPEN, item_id=item_id))
            continue
        if call.get("name") != DELEGATE_TOOL:
            continue
        intent, ctx = args.get("intent"), _context_of(args)
        if intent == INTENT_SEARCH:
            out.append(Event(INTENT_SEARCH, drive_query=str(ctx.get("drive_query") or "").strip()))
        elif intent == INTENT_OPEN:
            item_id = ref_item_id(str(ctx.get("file_ref") or ""))
            if item_id:
                out.append(Event(INTENT_OPEN, item_id=item_id))
    return out


def extract_events(rows: Iterable[Mapping[str, Any]]) -> List[Tuple[str, Event]]:
    """(trace_id, event) for every search and open in `rows` ({trace_id, timestamp, tool_calls}), ordered by trace,
    then timestamp, then position inside the row's call list. A row that is not JSON, or a call that is not a
    well-formed delegation, yields nothing."""
    ordered = sorted(enumerate(rows), key=lambda p: (str(p[1].get("trace_id") or ""),
                                                      str(p[1].get("timestamp") or ""), p[0]))
    out: List[Tuple[str, Event]] = []
    for _, row in ordered:
        trace = str(row.get("trace_id") or "")
        out.extend((trace, e) for e in _events_of_row(row.get("tool_calls")))
    return out


def pair_searches(rows: Iterable[Mapping[str, Any]]) -> List[SearchRecord]:
    """Each search with the files opened after it in the same trace and before the next search. A search with an
    empty `drive_query` (filter-only) is kept as a record with an empty query: the caller skips it."""
    searches: List[Tuple[str, str, List[str]]] = []
    current: Dict[str, int] = {}
    for trace, event in extract_events(rows):
        if event.kind == INTENT_SEARCH:
            searches.append((trace, event.drive_query, []))
            current[trace] = len(searches) - 1
        elif trace in current and event.item_id not in searches[current[trace]][2]:
            searches[current[trace]][2].append(event.item_id)
    return [SearchRecord(t, q, tuple(o)) for t, q, o in searches]


# -- concepts and what to ask the index ----------------------------------------------------------------------------

def concepts_of(drive_query: str) -> Tuple[Tuple[Concept, ...], bool, int, int]:
    """(concepts, name_only, ignored words, cut words). A query left with no concept is searched by the name stream
    alone over its name-word concepts (`name_word_concepts`); the real search does the same."""
    q = query_concepts(drive_query)
    if q.concepts:
        return q.concepts, False, len(q.ignored), len(q.cut)
    return name_word_concepts(drive_query), True, len(q.ignored), len(q.cut)


@dataclass(frozen=True)
class ProbeGroup:
    """One `limit 1` question to the index: does any record / chunk / hint document hold one of `keys`? The keys are
    of one family (`prefix`) in one stream, so their source class is fixed by the pair."""
    stream: str
    prefix: str
    source_class: SourceClass
    keys: Tuple[str, ...]


def _prefix(key: str) -> str:
    return key[:key.index(":") + 1] if ":" in key else ""


def _groups(stream: str, keys: Sequence[str], classify: Any) -> List[ProbeGroup]:
    by_prefix: Dict[str, List[str]] = {}
    for key in dict.fromkeys(keys):
        by_prefix.setdefault(_prefix(key), []).append(key)
    out: List[ProbeGroup] = []
    for prefix, group in by_prefix.items():
        cls = classify(group[0])
        if cls is None:      # `t:` trigrams and `l:` labels never cover a concept
            continue
        out.extend(ProbeGroup(stream, prefix, cls, batch) for batch in fetch_batches(group))
    return out


def probe_plan(concept: Concept, name_only: bool) -> List[ProbeGroup]:
    """The questions that decide how `concept` is held: the record stream's about keys, the name keys, the chunk
    streams's word keys and the hint keys, each cut per key family into lists of at most CONCEPT_KEYS_MAX. A
    name-only concept (`name_word_concepts`) is asked of the name stream alone."""
    name_keys = sorted(k for k in concept.word_keys if _prefix(k) in ("w:", "x:", "xl:", "n:"))
    name_keys += sorted(k for k in concept.about_keys if k.startswith("y:"))
    plan = _groups(STREAM_NAME, name_keys, source_class_of_name_key)
    if not name_only:
        plan += _groups(STREAM_ABOUT, concept.about_keys, source_class_of_about_key)
        plan += _groups(STREAM_CHUNK, concept.word_keys, source_class_of_word_key)
        plan += _groups(STREAM_HINT, concept.hint_keys, lambda _k: SourceClass.TRANSLATION)
    assert all(len(g.keys) <= CONCEPT_KEYS_MAX for g in plan)
    return plan


def status_of(held: Iterable[SourceClass]) -> str:
    """How a word is held: the strongest class any file holds it in; ABSENT when none does."""
    classes = set(held)
    if classes & _EXACT_CLASSES:
        return STATUS_EXACT
    if SourceClass.TRANSLATION in classes:
        return STATUS_TRANSLATION
    if SourceClass.AUTOMATIC_RECORD in classes:
        return STATUS_AUTOMATIC
    return STATUS_ABSENT


# -- per-word tallies --------------------------------------------------------------------------------------------

@dataclass
class WordTally:
    """How often one word was queried and how the index held it each time."""
    word: str
    is_number: bool
    queries: int = 0
    counts: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in STATUSES})

    def missed(self) -> int:
        """Queries where no file held the word exactly."""
        return self.queries - self.counts[STATUS_EXACT]


def tally_word(tallies: Dict[str, WordTally], concept: Concept, status: str) -> None:
    t = tallies.setdefault(concept.word, WordTally(concept.word, concept.is_number))
    t.queries += 1
    t.counts[status] += 1


def ranked_words(tallies: Mapping[str, WordTally]) -> List[WordTally]:
    """Words that were ever missed, most-missed first (ties: more queries, then the word). A profiler change is made
    for the words that repeat here, not for one query (Δ66)."""
    return sorted((t for t in tallies.values() if t.missed() > 0),
                  key=lambda t: (-t.missed(), -t.queries, t.word))


# -- the opened file -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ConceptCover:
    """How one concept is covered in the opened file: its best cover, or None when the file holds no key of it."""
    word: str
    source_class: Optional[SourceClass]
    where: str
    keys: Tuple[str, ...]

    @property
    def exact(self) -> bool:
        return self.source_class in _EXACT_CLASSES


@dataclass(frozen=True)
class OpenVerdict:
    """Whether the file the user meant was in an exact tier for the query, and the nearest keys of each concept it
    missed. Judged per concept, strongest cover anywhere in the file: the real tier also needs one chunk and the
    proximity window (§25.5.4), so a `True` here can still be a lower tier in the search; a `False` is certain."""
    item_id: str
    in_index: bool
    exact: bool
    covers: Tuple[ConceptCover, ...]

    def nearest(self) -> Tuple[ConceptCover, ...]:
        """The concepts not held exactly, with the nearest class that is held (None: no key at all)."""
        return tuple(c for c in self.covers if not c.exact)


def _best_cover(file: FileKeys, concept: Concept) -> Optional[Cover]:
    covers: List[Cover] = []
    rec = record_cover(file, concept)
    if rec is not None:
        covers.append(rec)
    for cid in sorted(set(file.chunk_keys) | set(file.hint_keys)):
        c = chunk_cover(file, concept, cid)
        if c is not None:
            covers.append(c)
    return min(covers, key=lambda c: CLASS_RANK[c.source_class]) if covers else None


def open_verdict(item_id: str, file: Optional[FileKeys], concepts: Sequence[Concept]) -> OpenVerdict:
    """`file` None: the opened item is not in the index (deleted since, or never carded)."""
    if file is None:
        return OpenVerdict(item_id, False, False, ())
    covers = []
    for concept in concepts:
        best = _best_cover(file, concept)
        covers.append(ConceptCover(concept.word, best.source_class if best else None,
                                   best.where if best else "", best.keys if best else ()))
    return OpenVerdict(file.item_id, True, all(c.exact for c in covers), tuple(covers))


def tier_gap(verdict: OpenVerdict) -> int:
    """How far from exact the file's weakest needed concept is, in classes: 0 exact, 1 translation, 2 automatic,
    3 a concept with no key at all. Used to order the report's list of misses."""
    if not verdict.covers:
        return 3
    worst = 0
    for c in verdict.covers:
        if c.source_class is None:
            return 3
        worst = max(worst, max(0, CLASS_RANK[c.source_class] - _EXACT_RANK))
    return worst


# -- report --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenedCase:
    """One search followed by an open: the query, the file's verdict. Item ids stay; names never enter the record."""
    drive_query: str
    verdict: OpenVerdict


def render_report(*, user_id: str, since: str, until: str, searches: int, queries_scored: int, skipped_filter_only: int,
                  tallies: Mapping[str, WordTally], opened: Sequence[OpenedCase], top: int = 60) -> str:
    """The markdown report. It names the user's query words and item ids: it is written to `scripts/memory/` only."""
    lines = [
        "# Drive query vocabulary (RFC §26.6, Δ66)", "",
        f"User `{user_id}`; window {since} .. {until or 'now'}.",
        f"Searches: {searches}; scored: {queries_scored}; filter-only (no drive_query): {skipped_filter_only}.", "",
        "## Words by misses", "",
        "A word is *missed* when no file holds it exactly (verified record or passage words). `absent` = df 0.", "",
        "| word | queries | absent | automatic only | translation only | exact |", "|---|---|---|---|---|---|",
    ]
    for t in ranked_words(tallies)[:top]:
        c = t.counts
        lines.append(f"| {t.word}{' (number)' if t.is_number else ''} | {t.queries} | {c[STATUS_ABSENT]} | "
                     f"{c[STATUS_AUTOMATIC]} | {c[STATUS_TRANSLATION]} | {c[STATUS_EXACT]} |")
    lines += ["", "## Searches followed by an open (the file the user meant)", ""]
    if not opened:
        lines.append("None in the window.")
    exact = [o for o in opened if o.verdict.exact]
    lines.append(f"Opened files: {len(opened)}; exact for every concept: {len(exact)}; "
                 f"not in the index: {sum(1 for o in opened if not o.verdict.in_index)}.")
    lines.append("")
    for o in sorted((o for o in opened if o.verdict.in_index and not o.verdict.exact),
                    key=lambda o: -tier_gap(o.verdict)):
        lines.append(f"- query `{o.drive_query}` opened `{o.verdict.item_id}`:")
        for c in o.verdict.nearest():
            held = f"{c.source_class.value} via {c.where} ({', '.join(c.keys[:3])})" if c.source_class else "no key"
            lines.append(f"  - `{c.word}`: {held}")
    lines += ["", "Judged per concept, strongest cover anywhere in the file; the search's own tier also needs one chunk "
              "and the proximity window, so \"exact\" above can still have shown lower.", ""]
    return "\n".join(lines)


def parse_since(value: str) -> str:
    """`YYYY-MM-DD`, validated; the BigQuery parameter is a DATE."""
    from datetime import date
    return date.fromisoformat(value).isoformat()
